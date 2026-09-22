import asyncio
import json
from pathlib import Path
from unittest import mock

import pytest

from workbot.agents.scheduler import AgentScheduler, AgentInvocationStopped
from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot


def make_bot(tmp_path: Path):
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {
            "cli": "welink-cli", "groups": [], "bootstrap_from_latest": True,
            "intent": {"enabled": True, "require_alias": True, "bot_aliases": ["bot", "WorkBot"]},
        },
        "ingestion_policy": {"default": "allow"},
        "reply_policy": {"default": "allow", "silent_if_unfulfillable": True},
        "execution_policy": {"default": "deny", "allow_senders": ["owner"]},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "rpc": {"enabled": False, "windows_read_roots": [str(tmp_path)]},
        "workspace_knowledge": {"enabled": False},
        "nodes": {},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = mock.AsyncMock()
    return bot


def self_dm(mid: str, text: str):
    return IncomingMessage(
        "welink", "welink:user:peer", "user", "peer", mid, "owner", text,
        int(mid), "Peer", from_self=True, transport="welinkbot"
    )


@pytest.mark.asyncio
async def test_manual_self_dm_can_address_workbot_but_plain_self_dm_is_store_only(tmp_path: Path):
    bot = make_bot(tmp_path)
    queued = []
    bot._enqueue_conversation_message = lambda m: queued.append((m.external_message_id, m.content, m.from_self))

    plain = self_dm("1", "我晚点回复你")
    addressed = self_dm("2", "bot 查询这个项目的README")
    assert await bot._ingest_im_message(plain) is True
    bot._dispatch_im_message(plain)
    assert await bot._ingest_im_message(addressed) is True
    bot._dispatch_im_message(addressed)

    rows = bot.conversations.recent_messages("welink:user:peer", limit=10)
    assert [r["direction"] for r in rows] == ["self", "self"]
    assert queued == [("2", "查询这个项目的README", True)]


@pytest.mark.asyncio
async def test_manual_self_dm_known_slash_command_bypasses_alias_in_strict_mode(tmp_path: Path):
    bot = make_bot(tmp_path)
    touched = []

    def fake_spawn(coro, **_kwargs):
        touched.append("spawn")
        coro.close()
        return mock.Mock(done=lambda: True)

    bot._spawn = fake_spawn
    plain = self_dm("3", "/sessions")
    assert await bot._ingest_im_message(plain) is True
    bot._dispatch_im_message(plain)
    assert touched == ["spawn"]

    addressed = self_dm("4", "bot /sessions")
    assert await bot._ingest_im_message(addressed) is True
    bot._dispatch_im_message(addressed)
    assert touched == ["spawn", "spawn"]


@pytest.mark.asyncio
async def test_scheduler_lists_and_cancels_waiting_without_killing_owner_task():
    sched = AgentScheduler(max_concurrent=1)
    release = asyncio.Event()
    waiter_stopped = asyncio.Event()

    async def holder():
        async with sched.slot(0, metadata={"purpose": "conversation", "conversation_id": "c1"}):
            await release.wait()

    async def waiter():
        try:
            async with sched.slot(0, metadata={"purpose": "conversation", "conversation_id": "c2"}):
                raise AssertionError("cancelled waiter must not enter slot")
        except AgentInvocationStopped:
            waiter_stopped.set()

    t1 = asyncio.create_task(holder(), name="holder")
    await asyncio.sleep(0)
    t2 = asyncio.create_task(waiter(), name="waiter")
    await asyncio.sleep(0)

    rows = sched.invocations()
    assert len(rows) == 2
    waiting = next(r for r in rows if r["state"] == "waiting")
    snap = sched.cancel(waiting["invocation_id"])
    assert snap and snap["conversation_id"] == "c2"
    await asyncio.wait_for(waiter_stopped.wait(), 1)
    assert not t2.cancelled()
    assert sched.stats().active == 1 and sched.stats().waiting == 0

    release.set()
    await t1
    await t2
    assert sched.stats().active == 0 and sched.stats().waiting == 0


@pytest.mark.asyncio
async def test_scheduler_has_no_phantom_active_slot_after_handoff_cancel():
    sched = AgentScheduler(max_concurrent=1)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def first():
        async with sched.slot(0):
            await release.wait()

    async def second():
        try:
            async with sched.slot(0):
                entered.set()
                await asyncio.sleep(10)
        except (AgentInvocationStopped, asyncio.CancelledError):
            pass

    t1 = asyncio.create_task(first())
    await asyncio.sleep(0)
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0)
    release.set()
    await t1
    # The waiter is now active. Stop exactly that registered invocation.
    for _ in range(20):
        rows = sched.invocations()
        active = [r for r in rows if r["state"] == "active"]
        if active:
            break
        await asyncio.sleep(0)
    assert active
    sched.cancel(active[0]["invocation_id"])
    await asyncio.wait_for(t2, 1)
    assert sched.stats().active == 0
    assert sched.stats().waiting == 0


def test_sessions_commands_are_fast_operator_controls(tmp_path: Path):
    bot = make_bot(tmp_path)
    assert bot._is_fast_control_message("/sessions") is True
    assert bot._is_fast_control_message("/stop-session agent-abcdef123456") is True
    assert bot._is_fast_control_message("/stop-sessions all") is True
    assert bot._is_operator_internal_command("/sessions") is True
    assert bot._is_operator_internal_command("/stop-session agent-abcdef123456") is True

@pytest.mark.asyncio
async def test_agent_manager_stop_cancels_only_backend_invocation_and_releases_slot(tmp_path: Path):
    from workbot.agents.manager import AgentManager
    from workbot.conversation.manager import ConversationManager
    from workbot.memory.manager import MemoryManager
    from workbot.storage.sqlite import Store

    store = Store(tmp_path / "mgr.sqlite")
    mgr = AgentManager({"command": "codeagent", "max_concurrent": 1}, tmp_path, ConversationManager(store), MemoryManager(store, {}))

    started = asyncio.Event()
    class BlockingBackend:
        async def run(self, prompt, **kwargs):
            started.set()
            await asyncio.sleep(3600)

    mgr.backend = BlockingBackend()

    async def invoke():
        with pytest.raises(AgentInvocationStopped):
            await mgr._backend_run("hello", priority=0, purpose="conversation", conversation_id="welink:user:x")

    outer = asyncio.create_task(invoke(), name="conversation-worker-test")
    await asyncio.wait_for(started.wait(), 1)
    rows = mgr.active_agent_sessions()
    assert len(rows) == 1 and rows[0]["state"] == "active"
    assert rows[0]["conversation_id"] == "welink:user:x"
    assert mgr.stop_agent_session(rows[0]["invocation_id"])
    await asyncio.wait_for(outer, 1)
    assert not outer.cancelled()
    assert mgr.scheduler_stats() == {"active": 0, "stopping": 0, "waiting": 0, "max_concurrent": 1}
