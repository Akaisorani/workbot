import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "linux"))

from workbot_worker.lifecycle import stop_daemon  # noqa: E402


class LinuxLifecycleTests(unittest.TestCase):
    def test_stop_cleans_stale_pid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "run"
            run.mkdir()
            (run / "worker.pid").write_text("99999999", encoding="utf-8")
            result = stop_daemon(root)
            self.assertTrue(result["ok"])
            self.assertEqual(result["reason"], "stale-pid")
            self.assertFalse((run / "worker.pid").exists())


if __name__ == "__main__":
    unittest.main()
