import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbot.agents.codeagent import AgentResult
from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot
from workbot.orchestration.models import WorkflowPlan, WorkflowStep


class FakeIM:
    def __init__(self):
        self.sent = []
    async def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))


class V03OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, *, access_control=None):
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        (root / "config").mkdir()
        cfg = {
            "database": "state/workbot.db",
            "im": {"cli": "welink-cli", "groups": [], "bootstrap_from_latest": True},
            "agent": {"command": "codeagent"},
            "default_node": "linux-server1",
            "nodes": {"linux-server1": {"ssh_alias": "linux-server1", "relay_command": "noop"}},
        }
        if access_control is not None:
            cfg["access_control"] = access_control
        path = root / "config" / "local.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        bot = WorkBot(path)
        bot.im = FakeIM()
        return td, bot

    def msg(self, mid, content, sender="owner-account"):
        return IncomingMessage(
            platform="welink", conversation_id="welink:group:g1", conversation_kind="group",
            external_conversation_id="g1", external_message_id=str(mid), sender_id=sender,
            content=content, sent_at_ms=int(mid), display_name="Example Group",
        )

    async def test_denied_sender_never_reaches_agent_or_conversation(self):
        td, bot = self.make_bot(access_control={"default": "deny", "allow_senders": ["owner"]})
        try:
            with mock.patch.object(bot.agents, "answer", new=mock.AsyncMock()) as answer:
                await bot.handle_im_message(self.msg(1, "hello", sender="other"))
                answer.assert_not_awaited()
            self.assertEqual(bot.im.sent, [])
            self.assertIsNone(bot.conversations.get("welink:group:g1"))
        finally:
            td.cleanup()

    async def test_mixed_request_proposes_workflow(self):
        td, bot = self.make_bot()
        try:
            plan = WorkflowPlan("combine", [
                WorkflowStep("local", "windows", "read local file"),
                WorkflowStep("remote", "remote", "read remote file", node="linux-server1"),
                WorkflowStep("compare", "windows", "compare", depends_on=["local", "remote"]),
            ])
            with mock.patch.object(bot.agents, "plan_workflow", new=mock.AsyncMock(return_value=plan)):
                await bot.handle_im_message(self.msg(
                    1, r"读取Windows本地D:\\x.txt，同时让linux-server1读取/tmp/y.txt，然后结合结果"
                ))
            pending = bot.conversations.get_pending_action("welink:group:g1")
            self.assertEqual(pending.action_type, "workflow.create")
            self.assertIn("local @ Windows", bot.im.sent[-1][1])
            self.assertIn("remote @ linux-server1", bot.im.sent[-1][1])
        finally:
            td.cleanup()

    async def test_independent_workflow_steps_run_concurrently_and_synthesize(self):
        td, bot = self.make_bot()
        try:
            plan = WorkflowPlan("two parallel inputs", [
                WorkflowStep("a", "windows", "A", notify_on_complete=True, milestone="A完成"),
                WorkflowStep("b", "remote", "B", node="linux-server1"),
                WorkflowStep("c", "windows", "C", depends_on=["a", "b"]),
            ])
            wid = bot.workflows.create(
                conversation_id="welink:group:g1", origin_message_id="1",
                instruction="original", plan=plan,
            )
            started = set()
            both_started = asyncio.Event()

            async def fake_step(workflow_id, conversation_id, step, results):
                if step.step_id in {"a", "b"}:
                    started.add(step.step_id)
                    if started == {"a", "b"}:
                        both_started.set()
                    await asyncio.wait_for(both_started.wait(), timeout=1)
                    await asyncio.sleep(0.01)
                if step.step_id == "c":
                    self.assertEqual(set(results), {"a", "b"})
                return f"result-{step.step_id}"

            with mock.patch.object(bot, "_run_workflow_step", side_effect=fake_step), \
                 mock.patch.object(bot.agents, "synthesize_workflow", new=mock.AsyncMock(return_value="final combined")):
                await bot._run_workflow(wid, "welink:group:g1", "original", plan)

            self.assertEqual(bot.workflows.get(wid)["state"], "completed")
            texts = [x[1] for x in bot.im.sent]
            self.assertTrue(any("阶段进展：A完成" in x for x in texts))
            self.assertTrue(any("工作流完成" in x and "final combined" in x for x in texts))
        finally:
            td.cleanup()

    async def test_reasoning_agent_can_propose_generic_remote_task(self):
        td, bot = self.make_bot()
        try:
            result = AgentResult(
                text="建议让服务器检查一下。",
                action={"type": "remote_task", "node": "linux-server1", "instruction": "检查 Feature X 是否有问题"},
            )
            with mock.patch.object(bot.agents, "answer", new=mock.AsyncMock(return_value=result)):
                await bot.handle_im_message(self.msg(1, "Feature X 有没有问题？"))
            pending = bot.conversations.get_pending_action("welink:group:g1")
            self.assertEqual(pending.action_type, "task.create")
            self.assertEqual(pending.payload["instruction"], "检查 Feature X 是否有问题")
            self.assertEqual(pending.payload["node"], "linux-server1")
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
