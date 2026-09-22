import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot
from workbot.transport.protocol import event


class FakeIM:
    def __init__(self):
        self.sent = []

    async def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))


class WorkBotRemoteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        (root / "config").mkdir()
        cfg = {
            "database": "state/workbot.db",
            "im": {"cli": "welink-cli", "groups": [], "bootstrap_from_latest": True},
            "agent": {"command": "codeagent"},
            "default_node": "linux-server1",
            "nodes": {"linux-server1": {"ssh_alias": "linux-server1", "relay_command": "noop"}},
        }
        path = root / "config" / "local.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        self.bot = WorkBot(path)
        self.bot.im = FakeIM()
        self.cid = "welink:group:1001"

    async def asyncTearDown(self):
        self.td.cleanup()

    def msg(self, mid, content):
        return IncomingMessage(
            platform="welink", conversation_id=self.cid, conversation_kind="group",
            external_conversation_id="1001", external_message_id=str(mid),
            sender_id="example-user-b", content=content, sent_at_ms=int(mid), display_name="Example Group",
        )

    async def test_acceptance_instruction_is_not_rewritten_as_integration_test(self):
        text = "在linux-server1创建一个测试任务，让codeagent在/tmp/workbot-test目录创建test.txt并写入hello world，完成后通知我"
        await self.bot.handle_im_message(self.msg(1, text))
        self.assertEqual(len(self.bot.im.sent), 1)
        reply = self.bot.im.sent[0][1]
        self.assertIn("将在 linux-server1 创建远端 CodeAgent 任务", reply)
        self.assertNotIn("集成测试", reply)
        pending = self.bot.conversations.get_pending_action(self.cid)
        self.assertEqual(pending.payload["instruction"], text)
        
        async def fake_create_remote_task(**kwargs):
            self.assertEqual(kwargs["instruction"], text)
            self.assertEqual(kwargs["title"], "远端任务")
            return "task-123"

        with mock.patch.object(self.bot.tasks, "create_remote_task", side_effect=fake_create_remote_task):
            await self.bot.handle_im_message(self.msg(2, "创建"))
        reply2 = self.bot.im.sent[-1][1]
        self.assertEqual(reply2, "已创建远端任务 task-123，正在 linux-server1 执行。")

    async def test_managed_agent_final_notification_is_not_duplicated(self):
        self.bot.store.execute(
            """INSERT INTO tasks(task_id, origin_conversation_id, origin_message_id, node, task_type, state, title, instruction)
               VALUES ('task-dup', ?, '1', 'linux-server1', 'codeagent', 'running', '远端任务', 'x')""",
            (self.cid,),
        )
        await self.bot._route_event(
            "linux-server1", event("agent.notification", task_id="task-dup", source="linux-server1", data={"summary": "agent says done"})
        )
        self.assertEqual(self.bot.im.sent, [])

        await self.bot._route_event(
            "linux-server1", event("task.completed", task_id="task-dup", source="linux-server1", data={"summary": "authoritative done"})
        )
        self.assertEqual(len(self.bot.im.sent), 1)
        self.assertIn("任务完成", self.bot.im.sent[0][1])
        self.assertIn("authoritative done", self.bot.im.sent[0][1])


if __name__ == "__main__":
    unittest.main()
