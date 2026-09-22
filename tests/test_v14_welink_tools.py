import json
import os
import subprocess
import sys
from pathlib import Path

from workbot.agents.codeagent import CodeAgentBackend
from workbot.tools.welink_capabilities import classify_welink_argv
from workbot.tools.welink_rate import SharedRateBudget

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "scripts" / "workbot-welink-gateway.py"


def test_capability_registry_read_write_and_format_flag():
    assert classify_welink_argv(["mail", "list"]).mode == "read"
    assert classify_welink_argv(["mail", "--format", "json", "list"]).action == "read.welink.mail"
    assert classify_welink_argv(["mail", "send", "--to", "x"]).action == "email.send"
    assert classify_welink_argv(["meeting", "create"]).action == "meeting.write"
    assert classify_welink_argv(["onebox", "node-search"]).mode == "read"
    assert classify_welink_argv(["onebox", "file-upload"]).action == "cloud.write"
    assert classify_welink_argv(["auth", "logout"]).mode == "admin"


def _fake_cli(tmp_path: Path) -> Path:
    if os.name == "nt":
        p = tmp_path / "fake-welink.cmd"
        p.write_text('@echo off\r\necho %*\r\n', encoding="utf-8")
        return p
    p = tmp_path / "fake-welink"
    p.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n', encoding="utf-8")
    p.chmod(0o755)
    return p


def _env(tmp_path: Path, real: Path, mode: str, token: Path | None = None):
    env = os.environ.copy()
    env.update({
        "WORKBOT_ROOT": str(ROOT),
        "WORKBOT_REAL_WELINK_CLI": str(real),
        "WORKBOT_WELINK_MODE": mode,
        "WORKBOT_WELINK_GATE_PATH": str(tmp_path / "gate"),
        "WORKBOT_WELINK_RATE_PATH": str(tmp_path / "rate.json"),
    })
    if token:
        env["WORKBOT_WELINK_APPROVAL_TOKEN"] = str(token)
    return env


def test_gateway_readonly_allows_read_and_denies_write(tmp_path: Path):
    real = _fake_cli(tmp_path)
    cp = subprocess.run([sys.executable, str(GATEWAY), "search", "person", "--text", "Example User"], env=_env(tmp_path, real, "readonly"), text=True, capture_output=True)
    assert cp.returncode == 0
    assert "search person" in cp.stdout
    cp = subprocess.run([sys.executable, str(GATEWAY), "mail", "send", "--to", "a@b", "--subject", "x"], env=_env(tmp_path, real, "readonly"), text=True, capture_output=True)
    assert cp.returncode != 0
    assert "read-only" in cp.stderr


def test_gateway_write_requires_one_use_matching_approval(tmp_path: Path):
    real = _fake_cli(tmp_path)
    env = _env(tmp_path, real, "execute")
    cp = subprocess.run([sys.executable, str(GATEWAY), "mail", "send", "--to", "a@b", "--subject", "x"], env=env, text=True, capture_output=True)
    assert cp.returncode == 4
    assert "approval required" in cp.stderr

    token = tmp_path / "token.json"
    token.write_text(json.dumps({"action": "email.send", "used": False}), encoding="utf-8")
    env = _env(tmp_path, real, "execute", token)
    cp = subprocess.run([sys.executable, str(GATEWAY), "mail", "send", "--to", "a@b", "--subject", "x"], env=env, text=True, capture_output=True)
    assert cp.returncode == 0
    assert json.loads(token.read_text())["used"] is True
    cp2 = subprocess.run([sys.executable, str(GATEWAY), "mail", "send", "--to", "a@b", "--subject", "y"], env=env, text=True, capture_output=True)
    assert cp2.returncode == 4


def test_gateway_token_must_match_action(tmp_path: Path):
    real = _fake_cli(tmp_path)
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"action": "im.send", "used": False}), encoding="utf-8")
    cp = subprocess.run([sys.executable, str(GATEWAY), "mail", "send", "--to", "a@b", "--subject", "x"], env=_env(tmp_path, real, "execute", token), text=True, capture_output=True)
    assert cp.returncode == 4
    assert json.loads(token.read_text())["used"] is False


def test_shared_rate_budget_is_shared_between_instances(tmp_path: Path):
    path = tmp_path / "rate.json"
    a = SharedRateBudget(path)
    b = SharedRateBudget(path)
    ok, _, used = a.try_reserve("im-history", limit=1, window_seconds=60, min_interval_seconds=0)
    assert ok and used == 1
    ok2, wait, used2 = b.try_reserve("im-history", limit=1, window_seconds=60, min_interval_seconds=0)
    assert not ok2 and wait > 0 and used2 == 1


def test_codeagent_managed_gateway_env(tmp_path: Path):
    backend = CodeAgentBackend({"command": sys.executable}, tmp_path)
    backend.configure_managed_welink(real_cli="C:/Tools/welink-cli.exe", gate_path="C:/state/gate", rate_path="C:/state/rate.json", trace=True)
    env = backend._subprocess_env(tool_mode="readonly")
    assert env["WORKBOT_WELINK_MODE"] == "readonly"
    assert env["WORKBOT_REAL_WELINK_CLI"] == "C:/Tools/welink-cli.exe"
    assert str(tmp_path / "scripts" / "tool-bin") in env["PATH"]
