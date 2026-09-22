import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbot.agents.codeagent import CodeAgentBackend


class CodeAgentBackendTests(unittest.TestCase):
    def test_resolves_bare_command_via_shutil_which(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WORKBOT_CODEAGENT_COMMAND", None)
                b = CodeAgentBackend({"command": "codeagent"}, Path(td))
                with mock.patch("workbot.agents.codeagent.shutil.which", return_value=r"C:\\tools\\codeagent.cmd"):
                    self.assertEqual(b._resolve_command(), r"C:\\tools\\codeagent.cmd")

    def test_env_command_overrides_config(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"WORKBOT_CODEAGENT_COMMAND": r"C:\\x\\codeagent.cmd"}):
                b = CodeAgentBackend({"command": "wrong"}, Path(td))
                self.assertEqual(b.command, r"C:\\x\\codeagent.cmd")


if __name__ == "__main__":
    unittest.main()

class WindowsBatchPromptFileTests(unittest.TestCase):
    def test_multiline_prompt_uses_prompt_file_for_windows_batch_shim(self):
        with tempfile.TemporaryDirectory() as td:
            b = CodeAgentBackend({"command": "codeagent"}, Path(td))
            prompt = "line one\nCURRENT USER REQUEST: read D:\\x.txt\nline three"
            with mock.patch("workbot.agents.codeagent.os.name", "nt"):
                self.assertTrue(b._needs_prompt_file(r"C:\\Program Files\\CodeAgentCLI\\codeagent.bat", prompt))
            path = b._write_prompt_file(prompt)
            try:
                self.assertEqual(path.read_text(encoding="utf-8"), prompt)
                bootstrap = b._bootstrap_for_prompt_file(path)
                self.assertNotIn("\n", bootstrap)
                self.assertIn(str(path), bootstrap)
                self.assertIn("Read that file completely FIRST", bootstrap)
            finally:
                path.unlink(missing_ok=True)

    def test_single_line_short_prompt_does_not_need_file_even_for_batch(self):
        with tempfile.TemporaryDirectory() as td:
            b = CodeAgentBackend({"command": "codeagent"}, Path(td))
            with mock.patch("workbot.agents.codeagent.os.name", "nt"):
                self.assertFalse(b._needs_prompt_file(r"C:\\CodeAgent\\codeagent.bat", "hello"))


def test_new_session_args_default():
    with tempfile.TemporaryDirectory() as td:
        b = CodeAgentBackend({"command":"codeagent"}, Path(td))
        assert b.new_session_args == ["--session-id", "{session_id}"]
