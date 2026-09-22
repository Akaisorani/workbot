from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import time
from pathlib import Path

root = Path(os.environ.get("WORKBOT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from workbot.tools.shared_gate import SharedDirectoryLock
from workbot.tools.welink_capabilities import classify_welink_argv
from workbot.tools.welink_rate import SharedRateBudget


def fail(msg: str, code: int = 3) -> int:
    print(msg, file=sys.stderr)
    return code


def consume_approval_token(path: str, action: str) -> bool:
    if not path:
        return False
    p = Path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if str(obj.get("action")) != action or bool(obj.get("used")):
        return False
    obj["used"] = True
    obj["used_at"] = time.time()
    try:
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True



def welink_launcher(real: str, argv: list[str]) -> list[str]:
    """Build a cross-platform launcher for the real WeLink CLI.

    On Windows npm-installed tools may resolve to .cmd/.bat shims, which cannot
    be passed to CreateProcess as native executables. Use cmd.exe explicitly.
    """
    resolved = shutil.which(real) or real
    suffix = Path(resolved).suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("ComSpec") or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
        command = subprocess.list2cmdline([resolved, *argv])
        return [comspec, "/d", "/s", "/c", command]
    if os.name == "nt" and suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved, *argv]
    return [resolved, *argv]


def main(argv: list[str]) -> int:
    real = os.environ.get("WORKBOT_REAL_WELINK_CLI", "").strip()
    if not real:
        return fail("WorkBot managed WeLink gateway: WORKBOT_REAL_WELINK_CLI is not configured")
    mode = os.environ.get("WORKBOT_WELINK_MODE", "disabled").strip().lower()
    cap = classify_welink_argv(argv)
    if mode == "disabled":
        return fail("WorkBot managed WeLink gateway: WeLink tools are disabled for this Agent invocation")
    if cap.mode in {"admin", "unknown"}:
        return fail(f"WorkBot managed WeLink gateway denies unsupported/admin operation: {cap.domain}.{cap.operation or '?'}")
    if cap.mode == "write":
        if mode != "execute":
            return fail(f"WorkBot Reply Policy is read-only; operation requires Execution Policy: {cap.action}")
        token = os.environ.get("WORKBOT_WELINK_APPROVAL_TOKEN", "")
        if not consume_approval_token(token, cap.action):
            return fail(
                f"WorkBot approval required before WeLink write action {cap.action}. "
                "Stop and emit <WORKBOT_APPROVAL> with this exact action; do not retry the CLI until WorkBot resumes after approval.",
                4,
            )

    gate_path = os.environ.get("WORKBOT_WELINK_GATE_PATH") or str(root / "state" / "welink-cli.gate")
    rate_path = os.environ.get("WORKBOT_WELINK_RATE_PATH") or str(root / "state" / "welink-rate.json")
    budget = SharedRateBudget(rate_path)
    if cap.rate_group == "im-history":
        budget.reserve_blocking("im-history", limit=18, window_seconds=60, min_interval_seconds=60/18, timeout=120)
    elif cap.rate_group == "onebox":
        # Documentation: each OneBox interface is user-rate-limited to 1 call/s.
        budget.reserve_blocking(f"onebox:{cap.operation}", limit=60, window_seconds=60, min_interval_seconds=1.0, timeout=120)
    elif cap.rate_group == "im-send":
        # FAQ recommends 30 seconds/message. Apply this to Agent-initiated sends;
        # WorkBot's own reply outbox has its own delivery/retry path.
        budget.reserve_blocking("agent-im-send", limit=2, window_seconds=60, min_interval_seconds=30.0, timeout=120)

    lock = SharedDirectoryLock(gate_path, timeout=120, stale_after=180)
    waited = lock.acquire()
    try:
        if os.environ.get("WORKBOT_WELINK_GATEWAY_TRACE") == "1":
            print(f"[WorkBot gateway] action={cap.action} mode={mode} gate_wait={waited:.3f}s", file=sys.stderr)
        cp = subprocess.run(welink_launcher(real, argv))
        return int(cp.returncode)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
