import tempfile
import unittest
from pathlib import Path

from workbot.storage.sqlite import Store
from workbot.tasks.manager import TaskManager
from workbot.transport.protocol import event


class EventDedupTests(unittest.TestCase):
    def test_duplicate_event_not_routed_twice(self):
        with tempfile.TemporaryDirectory() as td:
            tm = TaskManager(Store(Path(td) / "x.db"))
            e = event("agent.notification", source="linux-server1", data={"summary": "ok"})
            self.assertTrue(tm.handle_event("linux-server1", e))
            self.assertFalse(tm.handle_event("linux-server1", e))


if __name__ == "__main__":
    unittest.main()
