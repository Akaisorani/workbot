from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERRORS: list[str] = []


def fail(msg: str) -> None:
    ERRORS.append(msg)


def tracked_files() -> set[str] | None:
    if not (ROOT / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        return None
    return {x.decode("utf-8", errors="replace").replace("\\", "/") for x in proc.stdout.split(b"\0") if x}


tracked = tracked_files()

# Legacy development artifacts should not exist in the cleaned release tree.
for pattern in ["UPDATE_V*.md", "scripts/check-v*.ps1", "scripts/configure-v*.ps1", "scripts/test-v*.ps1"]:
    matches = list(ROOT.glob(pattern))
    if matches:
        fail(f"legacy release artifacts remain for {pattern}: " + ", ".join(str(p.relative_to(ROOT)) for p in matches[:8]))

# Runtime/private artifacts are allowed to exist locally, but must never be Git tracked.
if tracked is not None:
    forbidden_prefixes = (
        "state/", "logs/", "linux/state/", "linux/logs/", ".venv/", ".workbot-upgrade-backup/"
    )
    forbidden_exact = {"config/local.json", ".env"}
    for rel in sorted(tracked):
        if rel in forbidden_exact or rel.startswith(forbidden_prefixes):
            fail(f"private/runtime artifact is Git tracked: {rel}")
        if rel.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".pyc", ".log")):
            fail(f"runtime artifact is Git tracked: {rel}")

# Example config must parse and must not contain known development identifiers.
example = ROOT / "config/workbot.example.json"
try:
    json.loads(example.read_text(encoding="utf-8"))
except Exception as exc:
    fail(f"config/workbot.example.json is invalid JSON: {exc}")
raw = example.read_text(encoding="utf-8") if example.exists() else ""
for token in [
    "1002663911" + "82057691", "1004933330" + "908733613",
    "1005620488" + "812323850", "1000229336" + "600592637",
    "u000001", "p000002",
]:
    if token in raw:
        fail(f"development identifier leaked into example config: {token}")


# Generated AGENTS.md is local runtime state; the repository ships the template only.
if (ROOT / "README_ZH.md").exists():
    fail("README_ZH.md must not exist; README.md is the Chinese README")

# Scan source release for known environment-specific identifiers/paths.
SENSITIVE_TOKENS = [
    "h008" + "98895", "p300" + "67619", "1001678502" + "027711356",
    "1002663911" + "82057691", "1004933330" + "908733613",
    "1005620488" + "812323850", "1000229336" + "600592637",
    "wiki." + "huawei.com", "D:" + r"\code\dev_sql", "D:" + r"\工作文档",
    "韩" + "越", "彭" + "世瑜", "Han" + " Yue", "Mahesh" + " Dananjaya",
    "h005" + "17319", "m009" + "32418", "clouddrive." + "huawei.com/f/",
    "/home/" + "hanyue", "/home/" + "hy", "hanyue" + "_data", "hy" + "_data",
]
for path in ROOT.rglob("*"):
    if not path.is_file() or any(part in {".git", ".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
        continue
    if path.suffix.lower() in {".zip", ".png", ".jpg", ".jpeg", ".gif", ".db", ".sqlite", ".pyc"}:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        continue
    normalized_text = text.replace("\\\\", "\\")
    for token in SENSITIVE_TOKENS:
        if token in text or token in normalized_text:
            fail(f"sensitive environment token leaked in {path.relative_to(ROOT)}: {token}")

# Basic release metadata consistency.
pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE)
version = m.group(1) if m else ""
if not version:
    fail("cannot determine project version from pyproject.toml")
for doc in [ROOT / "README.md", ROOT / "README_EN.md"]:
    if version and version not in doc.read_text(encoding="utf-8"):
        fail(f"{doc.name} does not mention current version {version}")

required = [
    ".gitignore", "README.md", "README_EN.md", "CHANGELOG.md", "AGENTS.example.md",
    "config/workbot.example.json", "scripts/check-release.ps1", "scripts/setup-vision.ps1", "scripts/run-gui.ps1",
    "docs/rich-messages.md", "docs/gui.md", "tests", "workbot",
]
for rel in required:
    if not (ROOT / rel).exists():
        fail(f"required release item missing: {rel}")

ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
for token in ["state/", "logs/", "config/local.json", ".venv/", "*.db-wal", "*.db-shm"]:
    if token not in ignore:
        fail(f".gitignore missing required pattern: {token}")

if ERRORS:
    print("WORKBOT_RELEASE_HYGIENE_FAILED")
    for err in ERRORS:
        print(f"- {err}")
    sys.exit(1)

mode = "git-tracked" if tracked is not None else "source-tree"
print(f"WORKBOT_RELEASE_HYGIENE_OK version={version} mode={mode}")
