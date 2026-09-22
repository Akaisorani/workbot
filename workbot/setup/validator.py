from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .configurator import validate_config as validate_structure


def validate_config(config: dict[str, Any], *, workspace: str | Path = ".") -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for error in validate_structure(config):
        checks.append({"name": "config", "ok": False, "required": True, "detail": error})
    agent_cmd = str((config.get("agent") or {}).get("command") or "codeagent")
    checks.append({"name": "codeagent", "ok": bool(shutil.which(agent_cmd) or Path(agent_cmd).exists()), "required": True, "detail": agent_cmd})
    cli = str((config.get("im") or {}).get("cli") or "welink-cli")
    checks.append({"name": "welink-cli", "ok": bool(shutil.which(cli) or Path(cli).exists()), "required": True, "detail": cli})
    accounts = list((config.get("im") or {}).get("self_accounts") or [])
    checks.append({"name": "self-account", "ok": bool(accounts), "required": True, "detail": ", ".join(map(str, accounts)) or "not configured"})
    aliases = list((((config.get("im") or {}).get("intent") or {}).get("bot_aliases") or []))
    checks.append({"name": "bot-alias", "ok": bool(aliases), "required": True, "detail": ", ".join(map(str, aliases)) or "not configured"})
    return checks


def probe_ssh(alias: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        cp = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}", alias, "echo", "workbot-ok"], capture_output=True, text=True, timeout=timeout + 2)
        return cp.returncode == 0, (cp.stdout or cp.stderr).strip()[:300]
    except Exception as exc:
        return False, str(exc)
