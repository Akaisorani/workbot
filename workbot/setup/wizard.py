from __future__ import annotations

import argparse
from pathlib import Path

from .agents_generator import generate_agents_file
from .configurator import backup_config, build_default_config, load_config, merge_config, save_config_atomic, validate_config
from .doctor import run_doctor


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def configure_minimal(cfg: dict, *, quick: bool = False) -> dict:
    im = cfg.setdefault("im", {})
    intent = im.setdefault("intent", {})
    agent = cfg.setdefault("agent", {})
    account_default = str((im.get("self_accounts") or [""])[0])
    alias_default = str((intent.get("bot_aliases") or ["WorkBot"])[0])
    im["self_accounts"] = [_ask("Your WeLink account", account_default)]
    intent["bot_aliases"] = [_ask("Bot alias", alias_default)]
    agent["command"] = _ask("CodeAgent command", str(agent.get("command") or "codeagent"))
    if quick:
        return cfg
    if _ask("Configure a remote Linux node? (Y/n)", "Y").lower() not in {"n", "no"}:
        name = _ask("Node name", "linux-server1")
        ssh_alias = _ask("SSH alias", name)
        workspace = _ask("Workspace", "/home/workbot/workspace")
        cfg.setdefault("nodes", {})[name] = {"ssh_alias": ssh_alias, "workspace": workspace, "enabled": True}
        cfg["default_node"] = cfg.get("default_node") or name
    return cfg


def install(*, quick: bool = False, config_path: str = "config/local.json") -> int:
    root = Path.cwd(); path = root / config_path; example = root / "config" / "workbot.example.json"
    existing = load_config(path) if path.exists() else {}
    defaults = build_default_config(example if example.exists() else None)
    cfg = merge_config(defaults, existing)
    cfg = configure_minimal(cfg, quick=quick)
    errors = validate_config(cfg)
    if errors:
        print("Invalid config: " + "; ".join(errors)); return 2
    if path.exists():
        keep = int((cfg.get("setup") or {}).get("config_backup_count", 5)); backup_config(path, keep=keep)
    save_config_atomic(path, cfg)
    generate_agents_file(root / "AGENTS.example.md", cfg, root / "AGENTS.md")
    print("✓ config/local.json\n✓ AGENTS.md")
    result = run_doctor(path, workspace=root)
    return 0 if result.get("ok") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install"); p.add_argument("--quick", action="store_true"); p.add_argument("--config", default="config/local.json")
    p2 = sub.add_parser("configure"); p2.add_argument("--config", default="config/local.json")
    args = ap.parse_args(argv)
    return install(quick=getattr(args, "quick", False), config_path=args.config)

if __name__ == "__main__": raise SystemExit(main())
