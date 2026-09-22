import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage, PendingAction
from workbot.main import WorkBot
from workbot.orchestration.models import WorkflowPlan, WorkflowStep
from workbot.orchestration.scope import LOCAL_NODE, resolve_execution_scope
from workbot.storage.sqlite import Store


class FakeIM:
    def __init__(self):
        self.sent = []

    async def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))


NODE_CFG = {
    "linux-server1": {
        "ssh_alias": "linux-server1", "relay_command": "noop", "enabled": True,
        "capabilities": ["linux", "aarch64", "codeagent"],
    },
    "linux-server2": {
        "ssh_alias": "linux-server2", "relay_command": "noop", "enabled": True,
        "capabilities": ["linux", "x86_64", "codeagent"],
    },
}


class ScopeResolverTests(unittest.TestCase):
    def test_all_nodes_expands_windows_and_every_enabled_remote(self):
        scope = resolve_execution_scope("在我的所有节点上（包括windows）读取系统版本和内核版本", NODE_CFG)
        self.assertEqual(scope.targets, (LOCAL_NODE, "linux-server1", "linux-server2"))
        self.assertTrue(scope.multi_target)
        self.assertTrue(scope.requires_execution)
        self.assertTrue(scope.fresh_execution)

    def test_all_linux_nodes_excludes_windows(self):
        scope = resolve_execution_scope("在所有Linux节点检查uname", NODE_CFG)
        self.assertEqual(scope.targets, ("linux-server1", "linux-server2"))
        self.assertTrue(scope.multi_target)

    def test_explicit_windows_and_two_remote_nodes(self):
        scope = resolve_execution_scope("Windows、linux-server1和linux-server2分别读取内核版本", NODE_CFG)
        self.assertEqual(scope.targets, (LOCAL_NODE, "linux-server1", "linux-server2"))

    def test_capability_scopes_use_registry_metadata(self):
        x86 = resolve_execution_scope("在所有x86节点读取内核版本", NODE_CFG)
        arm = resolve_execution_scope("在所有ARM节点读取内核版本", NODE_CFG)
        self.assertEqual(x86.targets, ("linux-server2",))
        self.assertEqual(arm.targets, ("linux-server1",))

    def test_non_execution_node_question_does_not_force_workflow(self):
        scope = resolve_execution_scope("所有节点分别是什么？", NODE_CFG)
        self.assertTrue(scope.multi_target)
        self.assertFalse(scope.requires_execution)


class V11RoutingTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "config").mkdir()
        cfg = {
            "database": "state/workbot.db",
            "im": {"cli": "welink-cli", "groups": [], "bootstrap_from_latest": True},
            "agent": {"command": "codeagent"},
            "default_node": "linux-server1",
            "nodes": NODE_CFG,
        }
        path = root / "config" / "local.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        bot = WorkBot(path)
        bot.im = FakeIM()
        return td, bot

    @staticmethod
    def msg(mid, content):
        return IncomingMessage(
            platform="welink", conversation_id="welink:group:g1", conversation_kind="group",
            external_conversation_id="g1", external_message_id=str(mid), sender_id="owner",
            content=content, sent_at_ms=int(mid), display_name="Example Group",
        )

    async def test_all_nodes_query_bypasses_reasoning_agent_and_forces_one_workflow(self):
        td, bot = self.make_bot()
        try:
            plan = WorkflowPlan("read all kernels", [
                WorkflowStep("win", "windows", "read Windows kernel"),
                WorkflowStep("arm", "remote", "read kernel", node="linux-server1"),
                WorkflowStep("x86", "remote", "read kernel", node="linux-server2"),
            ])
            planner = mock.AsyncMock(return_value=plan)
            answer = mock.AsyncMock()
            with mock.patch.object(bot.agents, "plan_workflow", new=planner), \
                 mock.patch.object(bot.agents, "answer", new=answer):
                await bot.handle_im_message(self.msg(1, "在我的所有节点上（包括windows）读取系统版本（内核版本），汇总告诉我。"))
            answer.assert_not_awaited()
            planner.assert_awaited_once()
            kwargs = planner.await_args.kwargs
            self.assertEqual(kwargs["required_targets"], (LOCAL_NODE, "linux-server1", "linux-server2"))
            self.assertTrue(kwargs["fresh_execution"])
            pending = bot.conversations.get_pending_action("welink:group:g1")
            self.assertEqual(pending.action_type, "workflow.create")
            self.assertEqual(len(pending.payload["plan"]["steps"]), 3)
            text = bot.im.sent[-1][1]
            self.assertIn("多节点工作流", text)
            self.assertIn("@ Windows", text)
            self.assertIn("@ linux-server1", text)
            self.assertIn("@ linux-server2", text)
        finally:
            td.cleanup()

    async def test_reasoning_remote_task_is_overridden_when_action_instruction_is_multi_target(self):
        td, bot = self.make_bot()
        try:
            result = AgentResult(
                text="需要执行。",
                action={"type": "remote_task", "node": "linux-server1", "instruction": "让linux-server1和linux-server2分别检查当前内核版本"},
            )
            plan = WorkflowPlan("both", [
                WorkflowStep("a", "remote", "check", node="linux-server1"),
                WorkflowStep("b", "remote", "check", node="linux-server2"),
            ])
            with mock.patch.object(bot.agents, "answer", new=mock.AsyncMock(return_value=result)), \
                 mock.patch.object(bot.agents, "plan_workflow", new=mock.AsyncMock(return_value=plan)):
                await bot.handle_im_message(self.msg(1, "帮我检查两个开发架构"))
            pending = bot.conversations.get_pending_action("welink:group:g1")
            self.assertEqual(pending.action_type, "workflow.create")
        finally:
            td.cleanup()

    async def test_pending_cancel_is_consumed_without_reasoning_agent(self):
        td, bot = self.make_bot()
        try:
            bot.conversations.ensure(self.msg(0, "init"))
            pending = PendingAction("task.create", {"node": "linux-server1", "instruction": "x"}, "是否创建？")
            bot.conversations.set_pending_action("welink:group:g1", pending)
            answer = mock.AsyncMock()
            with mock.patch.object(bot.agents, "answer", new=answer):
                await bot.handle_im_message(self.msg(1, "否"))
            answer.assert_not_awaited()
            self.assertIsNone(bot.conversations.get_pending_action("welink:group:g1"))
            self.assertIn("已取消待执行操作", bot.im.sent[-1][1])
        finally:
            td.cleanup()


class WorkflowCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_target_triggers_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = Store(root / "state.db")
            manager = AgentManager({"command": "codeagent"}, root, ConversationManager(db))
            first = '<WORKBOT_PLAN>' + json.dumps({
                "summary": "partial",
                "steps": [
                    {"id": "w", "executor": "windows", "node": None, "instruction": "read win", "depends_on": []},
                    {"id": "h", "executor": "remote", "node": "linux-server1", "instruction": "read hy", "depends_on": []},
                ],
            }) + '</WORKBOT_PLAN>'
            repaired = '<WORKBOT_PLAN>' + json.dumps({
                "summary": "complete",
                "steps": [
                    {"id": "w", "executor": "windows", "node": None, "instruction": "read win", "depends_on": []},
                    {"id": "h", "executor": "remote", "node": "linux-server1", "instruction": "read hy", "depends_on": []},
                    {"id": "d", "executor": "remote", "node": "linux-server2", "instruction": "read dev", "depends_on": []},
                ],
            }) + '</WORKBOT_PLAN>'
            manager.backend.run = mock.AsyncMock(side_effect=[AgentResult(text=first), AgentResult(text=repaired)])
            plan = await manager.plan_workflow(
                "c1", "read all", {"linux-server1": {}, "linux-server2": {}},
                required_targets=(LOCAL_NODE, "linux-server1", "linux-server2"), fresh_execution=True,
            )
            self.assertEqual({s.node or LOCAL_NODE for s in plan.steps}, {LOCAL_NODE, "linux-server1", "linux-server2"})
            self.assertEqual(manager.backend.run.await_count, 2)
            repair_prompt = manager.backend.run.await_args_list[1].args[0]
            self.assertIn("Missing targets", repair_prompt)
            self.assertIn("linux-server2", repair_prompt)

    async def test_missing_target_after_repair_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = Store(root / "state.db")
            manager = AgentManager({"command": "codeagent"}, root, ConversationManager(db))
            partial = '<WORKBOT_PLAN>' + json.dumps({
                "summary": "partial",
                "steps": [{"id": "h", "executor": "remote", "node": "linux-server1", "instruction": "read", "depends_on": []}],
            }) + '</WORKBOT_PLAN>'
            manager.backend.run = mock.AsyncMock(side_effect=[AgentResult(text=partial), AgentResult(text=partial)])
            with self.assertRaisesRegex(ValueError, "未覆盖框架要求的节点"):
                await manager.plan_workflow(
                    "c1", "read both", {"linux-server1": {}, "linux-server2": {}},
                    required_targets=("linux-server1", "linux-server2"),
                )


if __name__ == "__main__":
    unittest.main()
