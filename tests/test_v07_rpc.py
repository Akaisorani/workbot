import asyncio
import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot
from workbot.rpc.manager import WindowsRPCManager
from workbot.transport.protocol import Message


class FakeConversations:
    def get(self, cid):
        return {"conversation_id": cid} if cid == "welink:group:g1" else None


class FakeAgents:
    def __init__(self):
        self.calls = []

    async def answer_node_request(self, **kwargs):
        self.calls.append(kwargs)
        return "windows answer"


@pytest.mark.asyncio
async def test_windows_rpc_read_is_root_constrained(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    f = root / "a.txt"
    f.write_text("hello rpc", encoding="utf-8")
    rpc = WindowsRPCManager(
        {"enabled": True, "allowed_nodes": ["linux-server1"], "windows_read_roots": [str(root)]},
        tmp_path, FakeAgents(), FakeConversations(),
    )
    result = await rpc.execute("linux-server1", "windows.fs.read", {"path": str(f)})
    assert result["content"] == "hello rpc"
    with pytest.raises(PermissionError):
        await rpc.execute("linux-server1", "windows.fs.read", {"path": str(tmp_path / "outside.txt")})
    with pytest.raises(PermissionError):
        await rpc.execute("other", "windows.fs.read", {"path": str(f)})


@pytest.mark.asyncio
async def test_workbot_query_preserves_origin_metadata(tmp_path: Path):
    agents = FakeAgents()
    rpc = WindowsRPCManager(
        {"enabled": True, "allowed_nodes": ["linux-server1"], "allowed_methods": ["workbot.query"]},
        tmp_path, agents, FakeConversations(),
    )
    result = await rpc.execute(
        "linux-server1", "workbot.query",
        {"instruction": "read company wiki", "conversation_id": "welink:group:g1"},
        task_id="task-123",
    )
    assert result["text"] == "windows answer"
    assert agents.calls[0]["node"] == "linux-server1"
    assert agents.calls[0]["task_id"] == "task-123"
    assert agents.calls[0]["conversation_id"] == "welink:group:g1"


class FakeIM:
    def __init__(self): self.sent = []
    async def send_text(self, cid, text): self.sent.append((cid, text))


def make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {"cli": "welink-cli", "groups": [], "bootstrap_from_latest": True},
        "agent": {"command": "codeagent"},
        "nodes": {"linux-server1": {"ssh_alias": "linux-server1", "relay_command": "noop"}},
        "rpc": {"enabled": True, "allowed_nodes": ["linux-server1"], "allowed_methods": ["workbot.query"]},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = FakeIM()
    return bot


@pytest.mark.asyncio
async def test_linux_rpc_does_not_block_remote_reader(tmp_path: Path):
    bot = make_bot(tmp_path)
    gate = asyncio.Event()

    async def slow_rpc(*args, **kwargs):
        await gate.wait()
        return {"text": "done"}

    bot.rpc.execute = slow_rpc
    bot.ssh.send = mock.AsyncMock()
    req = Message(type="request", method="workbot.query", source="linux-server1", target="workbot", data={"instruction": "x"})
    await asyncio.wait_for(bot.on_remote_message("linux-server1", req), timeout=0.2)
    # Handler returned while the RPC is still waiting.
    assert not bot.ssh.send.await_count
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bot.ssh.send.await_count == 1
    reply = bot.ssh.send.await_args.args[1]
    assert reply.type == "response"
    assert reply.correlation_id == req.id
    assert reply.data["ok"] is True


@pytest.mark.asyncio
async def test_different_conversations_dispatch_concurrently(tmp_path: Path):
    bot = make_bot(tmp_path)
    started = set()
    both = asyncio.Event()

    async def fake_process(msg):
        started.add(msg.conversation_id)
        if len(started) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1)

    bot._process_im_message = fake_process
    m1 = IncomingMessage("welink", "welink:group:a", "group", "a", "1", "u", "hello", 1, "a")
    m2 = IncomingMessage("welink", "welink:group:b", "group", "b", "2", "u", "hello", 2, "b")
    bot._dispatch_im_message(m1)
    bot._dispatch_im_message(m2)
    await asyncio.wait_for(both.wait(), timeout=1)
    assert started == {"welink:group:a", "welink:group:b"}
    for task in list(bot._background): task.cancel()
    await asyncio.gather(*list(bot._background), return_exceptions=True)


@pytest.mark.asyncio
async def test_same_conversation_dispatch_is_fifo(tmp_path: Path):
    bot = make_bot(tmp_path)
    order = []
    first_gate = asyncio.Event()

    async def fake_process(msg):
        order.append("start-" + msg.external_message_id)
        if msg.external_message_id == "1":
            await first_gate.wait()
        order.append("end-" + msg.external_message_id)

    bot._process_im_message = fake_process
    m1 = IncomingMessage("welink", "welink:group:a", "group", "a", "1", "u", "first", 1, "a")
    m2 = IncomingMessage("welink", "welink:group:a", "group", "a", "2", "u", "second", 2, "a")
    bot._dispatch_im_message(m1)
    bot._dispatch_im_message(m2)
    await asyncio.sleep(0.02)
    assert order == ["start-1"]
    first_gate.set()
    await asyncio.sleep(0.05)
    assert order == ["start-1", "end-1", "start-2", "end-2"]
    for task in list(bot._background): task.cancel()
    await asyncio.gather(*list(bot._background), return_exceptions=True)

@pytest.mark.asyncio
async def test_same_conversation_codeagent_session_is_serialized(tmp_path: Path):
    from workbot.agents.manager import AgentManager
    from workbot.conversation.manager import ConversationManager
    from workbot.storage.sqlite import Store
    from workbot.agents.codeagent import AgentResult

    store = Store(tmp_path / "state.db")
    conversations = ConversationManager(store)
    msg = IncomingMessage("welink", "welink:group:g1", "group", "g1", "1", "u", "seed", 1, "g1")
    conversations.add_incoming(msg)
    mgr = AgentManager({"command": "codeagent"}, tmp_path, conversations)
    active = 0
    maximum = 0
    release = asyncio.Event()

    async def fake_run(prompt, session_id=None, new_session_id=None):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await release.wait()
        active -= 1
        return AgentResult(text="ok", session_id=session_id or new_session_id, returncode=0)

    mgr.backend.run = fake_run
    a = asyncio.create_task(mgr._run_with_conversation_session("welink:group:g1", "a"))
    b = asyncio.create_task(mgr._run_with_conversation_session("welink:group:g1", "b"))
    await asyncio.sleep(0.02)
    assert maximum == 1
    release.set()
    await asyncio.gather(a, b)
