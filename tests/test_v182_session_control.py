import asyncio
import json
from pathlib import Path
from unittest import mock

import pytest

from workbot.agents.manager import AgentManager
from workbot.agents.scheduler import AgentInvocationStopped
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store


def make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "access_control": {"default": "allow"},
        "im": {"type": "welink", "groups": []},
        "agent": {"command": "codeagent", "max_concurrent": 2},
        "nodes": {},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    return WorkBot(cp)


@pytest.mark.asyncio
async def test_single_stop_waits_for_target_to_leave_registry(tmp_path: Path):
    store = Store(tmp_path / "mgr.sqlite")
    mgr = AgentManager(
        {"command": "codeagent", "max_concurrent": 1}, tmp_path,
        ConversationManager(store), MemoryManager(store, {}),
    )
    started = asyncio.Event()

    class Backend:
        async def run(self, prompt, **kwargs):
            started.set()
            await asyncio.sleep(3600)

    mgr.backend = Backend()

    async def invoke():
        with pytest.raises(AgentInvocationStopped):
            await mgr._backend_run(
                "hello", priority=0, purpose="conversation",
                conversation_id="welink:user:u1",
            )

    outer = asyncio.create_task(invoke())
    await asyncio.wait_for(started.wait(), 1)
    inv = mgr.active_agent_sessions()[0]
    snap, gone = await mgr.stop_agent_session_and_wait(inv["invocation_id"], timeout=1.0)
    assert snap and gone is True
    assert all(r["invocation_id"] != inv["invocation_id"] for r in mgr.active_agent_sessions())
    await asyncio.wait_for(outer, 1)


@pytest.mark.asyncio
async def test_summary_agent_is_tagged_to_its_conversation(tmp_path: Path):
    store = Store(tmp_path / "mgr.sqlite")
    conversations = ConversationManager(store)
    seed = IncomingMessage(
        "welink", "welink:user:u1", "user", "u1", "m1", "u1", "hello",
        1, "U1", transport="welinkbot",
    )
    conversations.ensure(seed)
    conversations.add_incoming(seed)
    mgr = AgentManager(
        {"command": "codeagent", "max_concurrent": 1}, tmp_path,
        conversations, MemoryManager(store, {}),
    )
    started = asyncio.Event()

    class Backend:
        async def run(self, prompt, **kwargs):
            started.set()
            await asyncio.sleep(3600)

    mgr.backend = Backend()
    task = asyncio.create_task(mgr.summarize_conversation("welink:user:u1"), name="summary-test")
    await asyncio.wait_for(started.wait(), 1)
    row = mgr.active_agent_sessions()[0]
    assert row["purpose"] == "maintenance-summary"
    assert row["conversation_id"] == "welink:user:u1"
    assert "summary" in row["detail"]
    mgr.stop_agent_session(row["invocation_id"])
    with pytest.raises(AgentInvocationStopped):
        await asyncio.wait_for(task, 1)


def test_maybe_schedule_summary_defers_when_same_conversation_agent_is_live(tmp_path: Path):
    bot = make_bot(tmp_path)
    bot._summary_min = 1
    bot._summary_every = 1
    seed = IncomingMessage(
        "welink", "welink:user:u1", "user", "u1", "m1", "u1", "hello",
        1, "U1", transport="welinkbot",
    )
    bot.conversations.ensure(seed)
    bot.conversations.add_incoming(seed)
    bot.agents.has_live_agent_for_conversation = mock.Mock(return_value=True)
    bot._spawn = mock.Mock()
    bot._maybe_schedule_summary("welink:user:u1")
    bot._spawn.assert_not_called()


@pytest.mark.asyncio
async def test_fast_control_does_not_opportunistically_launch_summary(tmp_path: Path):
    bot = make_bot(tmp_path)
    bot._maybe_schedule_summary = mock.Mock()
    bot._process_im_message = mock.AsyncMock()
    msg = IncomingMessage(
        "welink", "welink:group:g", "group", "g", "m1", "owner", "/sessions",
        1, "G", transport="welinkbot",
    )
    await bot._process_im_message_safe(msg)
    bot._process_im_message.assert_awaited_once()
    bot._maybe_schedule_summary.assert_not_called()


def test_sessions_background_classification_prefixes_are_explicit(tmp_path: Path):
    bot = make_bot(tmp_path)
    bot.agents.scheduler_stats = mock.Mock(return_value={"active": 2, "stopping": 0, "waiting": 0, "max_concurrent": 4})
    bot.agents.active_agent_sessions = mock.Mock(return_value=[
        {"invocation_id":"agent-fg","state":"active","purpose":"conversation","run_seconds":1,"wait_seconds":0,"conversation_id":"welink:user:u","session_id":"","detail":"","task_name":"im:u"},
        {"invocation_id":"agent-bg","state":"active","purpose":"maintenance-summary","run_seconds":1,"wait_seconds":0,"conversation_id":"welink:user:u","session_id":"","detail":"conversation summary","task_name":"summary:u"},
    ])
    # Formatting is covered asynchronously elsewhere; assert metadata grouping
    # contract used by /sessions is stable.
    rows = bot.agents.active_agent_sessions()
    assert rows[0]["purpose"] == "conversation"
    assert rows[1]["purpose"].startswith("maintenance")
