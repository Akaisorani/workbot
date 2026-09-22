from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .agents_generator import needs_regeneration
from .configurator import load_config, validate_config
from .validator import probe_ssh, validate_config as environment_checks


def run_doctor(config_path: str | Path = "config/local.json", *, workspace: str | Path = ".") -> dict:
    root = Path(workspace).resolve()
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = root / config_path
    checks: list[dict] = []
    def add(name: str, ok: bool, detail: str = "", *, required: bool = True, skipped: bool = False):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required, "skipped": skipped})
    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        import tkinter  # noqa: F401
        add("gui:tkinter", True, "available", required=False)
    except Exception as exc:
        add("gui:tkinter", False, str(exc), required=False)
    try:
        cfg = load_config(config_path)
        errors = validate_config(cfg)
        add("config", not errors, "; ".join(errors) or str(config_path))
    except Exception as exc:
        return {"ok": False, "internal_error": str(exc), "checks": checks, "exit_code": 2}
    for item in environment_checks(cfg, workspace=root):
        add(item["name"], item["ok"], item["detail"], required=item["required"])
    try:
        db = Path(cfg.get("database", "state/workbot.db"))
        if not db.is_absolute(): db = root / db
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("SELECT 1").fetchone(); conn.close()
        add("sqlite", True, str(db))
    except Exception as exc:
        add("sqlite", False, str(exc))
    for name, node in (cfg.get("nodes") or {}).items():
        if not isinstance(node, dict) or not bool(node.get("enabled", True)):
            continue
        alias = str(node.get("ssh_alias") or name)
        ok, detail = probe_ssh(alias)
        add(f"node:{name}", ok, detail, required=False)
    rag_enabled = bool((cfg.get("rag") or {}).get("enabled", False))
    add("rag", True, "enabled" if rag_enabled else "disabled", required=False, skipped=not rag_enabled)
    vision_enabled = bool(((((cfg.get("im") or {}).get("rich_message") or {}).get("image") or {}).get("enabled", False)))
    add("vision", True, "enabled" if vision_enabled else "disabled", required=False, skipped=not vision_enabled)
    try:
        template, output = root / "AGENTS.example.md", root / "AGENTS.md"
        ok = template.exists() and output.exists() and not needs_regeneration(template, cfg, output)
        add("agents", ok, "generated/current" if ok else "missing or stale")
    except Exception as exc:
        add("agents", False, str(exc))
    ok = all(c["ok"] for c in checks if c["required"] and not c["skipped"])
    return {"ok": ok, "checks": checks, "exit_code": 0 if ok else 1}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--config", default="config/local.json"); ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv); result = run_doctor(args.config)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("WorkBot Doctor")
        for c in result["checks"]:
            mark = "-" if c.get("skipped") else ("✓" if c["ok"] else "✗")
            print(f"{mark} {c['name']}: {c['detail']}")
    return int(result.get("exit_code", 2))

if __name__ == "__main__": raise SystemExit(main())
