import asyncio
import json
from pathlib import Path
from unittest import mock

import pytest

from workbot.agents.scheduler import AgentScheduler
from workbot.conversation.models import IncomingMessage
from workbot.im.welink import WeLinkAdapter
from workbot.main import WorkBot
from workbot.storage.sqlite import Store
from workbot.tasks.manager import TaskManager
from workbot.transport.protocol import Message
from workbot.memory.manager import MemoryManager
from linux.workbot_worker.daemon import WorkerDaemon


class DiscoveryWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__(
            "welink-cli", [], bootstrap_from_latest=False,
            discovery={"mode": "all", "refresh_seconds": 60, "max_conversations_per_poll": 10},
            self_accounts=["me"],
        )
        self.sent = []

    def _run_json(self, *args):
        if args[:2] == ("im", "query-recent-conversation"):
            return {
                "conversation_info": [
                    {"group_id": "g1", "group_name": "Project"},
                    {"user_account": "peer1", "user_name": "Peer"},
                ]
            }
        if "--group-id" in args:
            return {"respData": {"chatInfo": [
                {"content": "group hello", "contentType": "TEXT_MSG", "msgId": 10, "sender": "u1", "serverSendTime": 10},
            ]}}
        if "--user-account" in args:
            return {"respData": {"chatInfo": [
                {"content": "manual reply", "contentType": "TEXT_MSG", "msgId": 20, "sender": "me", "serverSendTime": 20},
                {"content": "peer question", "contentType": "TEXT_MSG", "msgId": 21, "sender": "peer1", "serverSendTime": 21},
            ]}}
        raise AssertionError(args)

    def _send_user_once(self, account: str, text: str):
        self.sent.append(("user", account, text))


@pytest.mark.asyncio
async def test_dynamic_discovery_reads_groups_and_private_messages_and_marks_self():
    a = DiscoveryWeLink()
    # V1.2.1 globally paces history fan-out. Drain two due conversations over
    # two poll slots instead of bursting both history calls at discovery time.
    msgs = await a.poll()
    a._next_history_query_at = 0.0
    msgs += await a.poll()
    assert {m.conversation_id for m in msgs} == {"welink:group:g1", "welink:user:peer1"}
    dm = [m for m in msgs if m.conversation_id == "welink:user:peer1"]
    assert len(dm) == 2
    assert dm[0].from_self is True
    assert dm[1].from_self is False
    await a.send_text("welink:user:peer1", "answer")
    assert a.sent and a.sent[0][1] == "peer1"


def make_v12_bot(tmp_path: Path, *, reply=None, execution=None, intent=None):
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {
            "cli": "welink-cli", "groups": [], "bootstrap_from_latest": True,
            "intent": intent or {"enabled": True, "bot_aliases": ["@测试用户", "WorkBot"], "ambient_batch_delay_seconds": 0.01},
        },
        "ingestion_policy": {"default": "allow"},
        "reply_policy": reply or {"default": "allow", "silent_if_unfulfillable": True},
        "execution_policy": execution or {"default": "deny", "allow_senders": ["owner"]},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "nodes": {"linux-server1": {"ssh_alias": "linux-server1", "relay_command": "noop"}},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = mock.AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_reply_only_private_message_silent_when_agent_cannot_help(tmp_path: Path):
    bot = make_v12_bot(tmp_path)
    msg = IncomingMessage("welink", "welink:user:peer", "user", "peer", "1", "peer", "帮我执行程序", 1, "Peer")
    bot.agents.answer_read_only = mock.AsyncMock(return_value={"can_reply": False, "confidence": 0.9, "answer": "", "reason": "needs execution"})
    bot._send_text = mock.AsyncMock()
    await bot.handle_im_message(msg)
    bot.agents.answer_read_only.assert_awaited_once()
    bot._send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_only_private_message_answers_when_read_only_agent_can_solve(tmp_path: Path):
    bot = make_v12_bot(tmp_path)
    msg = IncomingMessage("welink", "welink:user:peer", "user", "peer", "1", "peer", "Wiki里的发布流程是什么？", 1, "Peer")
    bot.agents.answer_read_only = mock.AsyncMock(return_value={"can_reply": True, "confidence": 0.92, "answer": "发布流程答案", "reason": "found wiki"})
    bot._send_text = mock.AsyncMock()
    await bot.handle_im_message(msg)
    bot._send_text.assert_awaited_once_with("welink:user:peer", "发布流程答案")


@pytest.mark.asyncio
async def test_group_direct_mention_is_immediate_but_chatter_does_not_call_agent(tmp_path: Path):
    bot = make_v12_bot(tmp_path)
    queued = []
    bot._enqueue_conversation_message = lambda msg: queued.append(msg.external_message_id)
    direct = IncomingMessage("welink", "welink:group:g", "group", "g", "1", "peer", "@测试用户\u2005你好，帮我查一下Wiki", 1, "g")
    chatter = IncomingMessage("welink", "welink:group:g", "group", "g", "2", "peer", "午饭吃什么", 2, "g")
    bot._dispatch_im_message(direct)
    bot._dispatch_im_message(chatter)
    assert queued == ["1"]
    assert not bot._ambient_batches


@pytest.mark.asyncio
async def test_group_ambient_question_is_batched_and_model_selects_only_bot_intent(tmp_path: Path):
    bot = make_v12_bot(tmp_path)
    selected = []
    bot._enqueue_conversation_message = lambda msg: selected.append(msg.external_message_id)
    bot.agents.classify_group_intents = mock.AsyncMock(return_value=["2"])
    m1 = IncomingMessage("welink", "welink:group:g", "group", "g", "1", "peer", "这个接口为什么慢？", 1, "g")
    m2 = IncomingMessage("welink", "welink:group:g", "group", "g", "2", "peer", "能不能让WorkBot帮忙查一下？", 2, "g")
    # m2 includes alias and is direct, so use an unaliased request for actual batch selection.
    m2.content = "能不能让助手帮忙查一下？"
    bot._dispatch_im_message(m1)
    bot._dispatch_im_message(m2)
    await bot._flush_ambient_batch("welink:group:g")
    assert selected == ["2"]
    assert bot.agents.classify_group_intents.await_count == 1


@pytest.mark.asyncio
async def test_pending_confirmation_bypasses_group_ambient_batch(tmp_path: Path):
    bot = make_v12_bot(tmp_path)
    seed = IncomingMessage("welink", "welink:group:g", "group", "g", "0", "owner", "seed", 0, "g")
    bot.conversations.add_incoming(seed)
    from workbot.conversation.models import PendingAction
    bot.conversations.set_pending_action("welink:group:g", PendingAction("task.create", {"node": "linux-server1", "instruction": "x"}, "confirm"))
    queued = []
    bot._enqueue_conversation_message = lambda msg: queued.append(msg.external_message_id)
    confirm = IncomingMessage("welink", "welink:group:g", "group", "g", "1", "owner", "执行", 1, "g")
    bot._dispatch_im_message(confirm)
    assert queued == ["1"]


@pytest.mark.asyncio
async def test_agent_scheduler_prefers_interactive_waiter_over_background_waiter():
    scheduler = AgentScheduler(1)
    order = []
    release = asyncio.Event()

    async def holder():
        async with scheduler.slot(10):
            order.append("holder")
            await release.wait()

    async def waiter(name, priority):
        async with scheduler.slot(priority):
            order.append(name)

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    low = asyncio.create_task(waiter("low", 80))
    high = asyncio.create_task(waiter("high", 0))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(h, low, high)
    assert order == ["holder", "high", "low"]


def test_terminal_task_ignores_late_progress_and_conflicting_completion(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    store.execute("INSERT INTO tasks(task_id,node,task_type,state,title,instruction) VALUES ('task-x','linux-server1','codeagent','cancelled','x','x')")
    tm = TaskManager(store)
    progress = Message(type="event", id="evt-late-progress", event="task.progress", task_id="task-x", data={"summary": "late"})
    complete = Message(type="event", id="evt-late-complete", event="task.completed", task_id="task-x", data={"summary": "late done"})
    assert tm.handle_event("linux-server1", progress) is False
    assert tm.handle_event("linux-server1", complete) is False
    assert tm.get("task-x")["state"] == "cancelled"


class FakeWriter:
    def __init__(self): self.data = b""
    def write(self, data): self.data += data
    async def drain(self): pass


@pytest.mark.asyncio
async def test_linux_worker_drops_late_local_event_for_terminal_task(tmp_path: Path):
    daemon = WorkerDaemon(tmp_path)
    daemon.store.execute(
        "INSERT INTO tasks(task_id,task_type,state) VALUES ('task-x','codeagent','cancelled')"
    )
    writer = FakeWriter()
    msg = Message(type="event", id="evt-late", event="task.progress", task_id="task-x", data={"summary":"late"})
    await daemon._handle_local(msg, writer)
    assert daemon.store.pending() == []
    ack = json.loads(writer.data.decode().strip())
    assert ack["data"]["dropped"] is True


def test_conversation_auto_memory_becomes_promotion_candidate(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    mm = MemoryManager(store, {})
    mid = mm.remember("conversation:welink:group:g", "repo", "example_repo is the main repo", source="conversation-batch:g", auto_generated=True, evidence="example_repo is the main repo")
    row = mm.get(mid)
    assert row["promotion_state"] == "candidate"
    assert [r["id"] for r in mm.promotion_candidates()] == [mid]

@pytest.mark.asyncio
async def test_reply_only_user_cannot_read_workbot_internal_commands(tmp_path: Path):
    bot = make_v12_bot(tmp_path)
    msg = IncomingMessage("welink", "welink:user:peer", "user", "peer", "99", "peer", "/nodes", 99, "Peer")
    bot._send_text = mock.AsyncMock()
    bot.agents.answer_read_only = mock.AsyncMock()
    await bot.handle_im_message(msg)
    bot._send_text.assert_not_awaited()
    bot.agents.answer_read_only.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_capture_never_auto_promotes_global_even_with_legacy_auto_global(tmp_path: Path):
    bot = make_v12_bot(tmp_path)
    bot.memory_cfg["auto_global"] = True
    bot.agents.extract_memories = mock.AsyncMock(return_value=[])
    await bot._capture_memories(
        "welink:group:g", "stable looking statement", source="conversation-batch:g", source_kind="conversation"
    )
    assert bot.agents.extract_memories.await_args.kwargs["allow_global"] is False


@pytest.mark.asyncio
async def test_workflow_proposal_renders_framework_authoritative_targets(tmp_path: Path):
    from workbot.orchestration.models import WorkflowPlan, WorkflowStep
    from workbot.orchestration.scope import ExecutionScope

    bot = make_v12_bot(tmp_path)
    bot.cfg["nodes"]["linux-server2"] = {"ssh_alias": "linux-server2", "relay_command": "noop"}
    # NodeRegistry was built before the extra config; this test only needs the planner mock and scope.
    bot.agents.plan_workflow = mock.AsyncMock(return_value=WorkflowPlan(
        summary="在 office-pc、linux-server1、example-server3 查询版本",  # deliberately wrong prose node name
        steps=[
            WorkflowStep("step-1", "windows", "read Windows version"),
            WorkflowStep("step-2", "remote", "read linux-server1 version", node="linux-server1"),
            WorkflowStep("step-3", "remote", "read linux-server2 version", node="linux-server2"),
        ],
    ))
    bot._send_text = mock.AsyncMock()
    msg = IncomingMessage("welink", "welink:group:g", "group", "g", "100", "owner", "所有节点查版本", 100, "g")
    scope = ExecutionScope(targets=("office-pc", "linux-server1", "linux-server2"), source="all_nodes", requires_execution=True, fresh_execution=True)
    await bot._propose_workflow(msg, msg.content, scope=scope)
    sent = bot._send_text.await_args.args[1]
    assert "目标节点：office-pc, linux-server1, linux-server2" in sent
