from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


def load_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def build_default_config(example_path: str | Path | None = None) -> dict[str, Any]:
    if example_path and Path(example_path).exists():
        return load_config(example_path)
    return {
        "database": "state/workbot.db",
        "im": {"cli": "welink-cli", "self_accounts": [], "intent": {"bot_aliases": ["WorkBot"], "require_alias": True}},
        "agent": {"command": "codeagent", "cwd": ".", "timeout_seconds": 900},
        "nodes": {},
        "setup": {"config_backup_count": 5},
    }


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def validate_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["config must be a JSON object"]
    if not isinstance(config.get("im", {}), dict):
        errors.append("im must be an object")
    if not isinstance(config.get("agent", {}), dict):
        errors.append("agent must be an object")
    if not isinstance(config.get("nodes", {}), dict):
        errors.append("nodes must be an object")
    aliases = (((config.get("im") or {}).get("intent") or {}).get("bot_aliases") or [])
    if aliases and not all(isinstance(x, str) and x.strip() for x in aliases):
        errors.append("im.intent.bot_aliases must contain non-empty strings")
    collaboration = config.get("collaboration") or {}
    if not isinstance(collaboration, dict):
        errors.append("collaboration must be an object")
    elif bool(collaboration.get("enabled", False)):
        agent_id = str(collaboration.get("agent_id") or "").strip()
        self_accounts = [str(x) for x in ((config.get("im") or {}).get("self_accounts") or []) if str(x)]
        if not self_accounts:
            # Runtime identity and self-echo suppression both need the real IM
            # account. agent_id itself may be derived automatically from it.
            errors.append("im.self_accounts must be explicitly configured for multi-Agent collaboration")
        collab_groups = [str(x) for x in (collaboration.get("groups") or []) if str(x)]
        im_groups = [str(x.get("group_id") or "") for x in (((config.get("im") or {}).get("groups")) or []) if isinstance(x, dict) and str(x.get("group_id") or "")]
        if not collab_groups and not im_groups:
            errors.append("collaboration requires at least one collaboration.groups entry or configured im.groups group")
        peers = collaboration.get("peers") or {}
        if not isinstance(peers, dict):
            errors.append("collaboration.peers must be an object")
        else:
            if agent_id and agent_id in peers:
                errors.append("collaboration.peers must not contain the local agent_id")
            seen_accounts: set[str] = set()
            discovery_enabled = bool(
                collaboration.get("discovery_enabled", collaboration.get("auto_discovery", True))
            )
            for peer_id, peer_cfg in peers.items():
                if not str(peer_id).strip() or not isinstance(peer_cfg, dict):
                    errors.append("collaboration.peers entries must be named objects")
                    continue
                accounts = [str(x) for x in (peer_cfg.get("sender_accounts") or []) if str(x)]
                if not accounts and not discovery_enabled:
                    errors.append(f"collaboration.peers.{peer_id}.sender_accounts is required when discovery_enabled=false")
                for account in accounts:
                    if account in seen_accounts:
                        errors.append(f"collaboration peer sender account is duplicated: {account}")
                    seen_accounts.add(account)
    return errors


def backup_config(path: str | Path, *, keep: int = 5) -> Path | None:
    p = Path(path)
    if not p.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = p.with_name(f"{p.name}.bak-{stamp}")
    shutil.copy2(p, target)
    backups = sorted(p.parent.glob(f"{p.name}.bak-*"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in backups[max(0, int(keep)):]:
        old.unlink(missing_ok=True)
    return target


def save_config_atomic(path: str | Path, config: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    data = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
