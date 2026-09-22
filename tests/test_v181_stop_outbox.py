import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from workbot.agents.codeagent import CodeAgentBackend
from workbot.agents.manager import AgentManager
from workbot.agents.scheduler import AgentInvocationStopped
from workbot.conversation.manager import ConversationManager
from workbot.im.welink import WeLinkAdapter
from workbot.main import WorkBot
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store


class SlowWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__("welink-cli", [], bootstrap_from_latest=False)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def send_text(self, conversation_id: str, text: str, *, created_at_ms=None, verify_before_send=False):
        self.calls += 1
        self.started.set()
        await self.release.wait()


def make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "access_control": {"default": "allow"},
        "im": {"type": "welink", "groups": []},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "nodes": {},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    return WorkBot(cp)


@pytest.mark.asyncio
async def test_scheduler_stop_becomes_stopping_immediately_and_repeat_is_idempotent(tmp_path: Path):
    store = Store(tmp_path / "mgr.sqlite")
    mgr = AgentManager(
        {"command": "codeagent", "max_concurrent": 1}, tmp_path,
        ConversationManager(store), MemoryManager(store, {}),
    )

    started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class SlowCancelBackend:
        async def run(self, prompt, **kwargs):
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await cleanup_release.wait()
                raise

    mgr.backend = SlowCancelBackend()

    async def invoke():
        with pytest.raises(AgentInvocationStopped):
            await mgr._backend_run("hello", priority=0, purpose="conversation", conversation_id="welink:user:x")

    outer = asyncio.create_task(invoke())
    await asyncio.wait_for(started.wait(), 1)
    row = mgr.active_agent_sessions()[0]
    first = mgr.stop_agent_session(row["invocation_id"])
    assert first and first["state"] == "stopping"
    assert first["previous_state"] == "active"
    assert mgr.scheduler_stats() == {"active": 0, "stopping": 1, "waiting": 0, "max_concurrent": 1}
    listed = mgr.active_agent_sessions()
    assert len(listed) == 1 and listed[0]["state"] == "stopping"

    second = mgr.stop_agent_session(row["invocation_id"])
    assert second and second["already_stopping"] is True
    assert mgr.scheduler_stats()["stopping"] == 1

    cleanup_release.set()
    await asyncio.wait_for(outer, 1)
    assert mgr.scheduler_stats() == {"active": 0, "stopping": 0, "waiting": 0, "max_concurrent": 1}


@pytest.mark.asyncio
async def test_outbound_immediate_send_cannot_race_retry_loop(tmp_path: Path):
    bot = make_bot(tmp_path)
    slow = SlowWeLink()
    bot.im = slow

    send_task = asyncio.create_task(bot._send_text("welink:user:u1", "hello"))
    await asyncio.wait_for(slow.started.wait(), 1)
    row = bot.store.query_one("SELECT * FROM outbound_messages ORDER BY created_at_ms DESC LIMIT 1")
    assert row["state"] == "sending"

    # Simulate the background outbox observing the same row. Atomic claim must
    # fail because the immediate sender already owns state=sending.
    won = await bot._attempt_outbound(
        row["send_id"], row["conversation_id"], row["text"], row["created_at_ms"],
        verify_before_send=True,
    )
    assert won is False
    assert slow.calls == 1

    slow.release.set()
    await asyncio.wait_for(send_task, 1)
    done = bot.store.query_one("SELECT state FROM outbound_messages WHERE send_id=?", (row["send_id"],))
    assert done["state"] == "delivered"
    assert slow.calls == 1


def test_workbot_startup_requeues_interrupted_sending_row(tmp_path: Path):
    bot = make_bot(tmp_path)
    bot.store.execute(
        "INSERT INTO outbound_messages(send_id,conversation_id,text,state,created_at_ms) VALUES ('s1','welink:user:u','x','sending',1)"
    )
    # A new WorkBot process must recover an interrupted sending claim as pending
    # so retry will use history verification before another send.
    bot2 = WorkBot(tmp_path / "config" / "local.json")
    row = bot2.store.query_one("SELECT state FROM outbound_messages WHERE send_id='s1'")
    assert row["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression test")
async def test_codeagent_cancel_reaps_spawned_process_group(tmp_path: Path):
    script = tmp_path / "fake-codeagent.sh"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 3600 &\n"
        "echo $! > child.pid\n"
        "wait\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    backend = CodeAgentBackend({"command": str(script), "prompt_args": [], "timeout_seconds": 30}, tmp_path)
    task = asyncio.create_task(backend.run("hello"))
    child_file = tmp_path / "child.pid"
    for _ in range(100):
        if child_file.exists():
            break
        await asyncio.sleep(0.01)
    assert child_file.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)

@pytest.mark.asyncio
async def test_operator_stopped_conversation_is_silent_not_failure_reply(tmp_path: Path):
    from unittest import mock
    from workbot.conversation.models import IncomingMessage

    bot = make_bot(tmp_path)
    bot.agents.answer = mock.AsyncMock(side_effect=AgentInvocationStopped("agent-x stopped by operator"))
    bot._send_text = mock.AsyncMock()
    msg = IncomingMessage(
        "welink", "welink:user:u1", "user", "u1", "m1", "u1", "hello",
        1, "U1", transport="welinkbot",
    )
    await bot._process_im_message(msg)
    bot._send_text.assert_not_awaited()

@pytest.mark.asyncio
async def test_windows_process_tree_cleanup_uses_taskkill(monkeypatch, tmp_path: Path):
    import workbot.agents.codeagent as codeagent_mod

    class FakeProc:
        pid = 4242
        returncode = None
        killed = False
        def kill(self):
            self.killed = True
            self.returncode = -9
        async def wait(self):
            return self.returncode

    class FakeKiller:
        returncode = None
        async def wait(self):
            self.returncode = 0
            return 0
        def kill(self):
            self.returncode = -9

    calls = []
    async def fake_create(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeKiller()

    monkeypatch.setattr(codeagent_mod.os, "name", "nt")
    monkeypatch.setattr(codeagent_mod.shutil, "which", lambda x: "C:/Windows/System32/taskkill.exe" if "taskkill" in x else None)
    monkeypatch.setattr(codeagent_mod.asyncio, "create_subprocess_exec", fake_create)

    backend = CodeAgentBackend({"command": sys.executable}, tmp_path)
    proc = FakeProc()
    await backend._terminate_process_tree(proc)
    assert calls
    args = calls[0][0]
    assert args[:6] == ("C:/Windows/System32/taskkill.exe", "/PID", "4242", "/T", "/F")[:6]
