import tempfile
import unittest
from pathlib import Path

from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage, PendingAction
from workbot.storage.sqlite import Store


class ConversationTests(unittest.TestCase):
    def test_pending_action_persists(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "x.db")
            cm = ConversationManager(store)
            msg = IncomingMessage("welink", "welink:group:1", "group", "1", "10", "u", "创建测试", 1, "g")
            self.assertTrue(cm.add_incoming(msg))
            cm.set_pending_action(msg.conversation_id, PendingAction("task.create", {"node": "linux-server1"}, "确认？"))
            p = cm.get_pending_action(msg.conversation_id)
            self.assertEqual(p.action_type, "task.create")
            self.assertEqual(p.payload["node"], "linux-server1")


if __name__ == "__main__":
    unittest.main()
