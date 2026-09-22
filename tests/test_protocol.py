import unittest
from workbot.transport.protocol import Message, request


class ProtocolTests(unittest.TestCase):
    def test_round_trip_unicode(self):
        m = request("task.create", task_id="task-1", data={"instruction": "运行测试"})
        r = Message.loads(m.dumps())
        self.assertEqual(r.type, "request")
        self.assertEqual(r.method, "task.create")
        self.assertEqual(r.task_id, "task-1")
        self.assertEqual(r.data["instruction"], "运行测试")


if __name__ == "__main__":
    unittest.main()
