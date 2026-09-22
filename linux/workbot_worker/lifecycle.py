from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def socket_alive(socket_path: Path, timeout: float = 0.3) -> bool:
    if not socket_path.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path))
        f = s.makefile("rwb", buffering=0)
        f.write((json.dumps({"role": "probe", "pid": os.getpid()}) + "\n").encode())
        return bool(f.readline())
    except OSError:
        return False
    finally:
        s.close()


def ensure_daemon(root: Path, wait_seconds: float = 10.0) -> dict:
    """Ensure the node-local worker daemon exists independently of SSH.

    V0.1.1 deliberately does not require systemd --user/linger.  The daemon is
    detached from the invoking SSH/session and redirects all stdio to a log.
    Relay and agentctl may call this repeatedly; the daemon itself also holds a
    single-instance lock.
    """
    root = root.expanduser().resolve()
    run_dir = root / "run"
    logs_dir = root / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    socket_path = run_dir / "worker.sock"
    if socket_alive(socket_path):
        return {"ok": True, "started": False, "socket": str(socket_path)}

    # Serialize competing relay/agentctl startup attempts with an advisory
    # file lock. Unlike a lock directory, this cannot be left stale by a crash.
    start_lock_path = run_dir / "start.lock"
    try:
        import fcntl  # POSIX-only; keep module importable on Windows for diagnostics/tests.
    except ImportError as exc:
        raise RuntimeError("agent-worker lifecycle startup requires a POSIX/Linux environment") from exc
    lockf = start_lock_path.open("a+")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if socket_alive(socket_path):
                lockf.close()
                return {"ok": True, "started": False, "socket": str(socket_path)}
            if time.monotonic() >= deadline:
                lockf.close()
                raise RuntimeError(f"timed out waiting for worker startup lock: {start_lock_path}")
            time.sleep(0.1)

    try:
        if socket_alive(socket_path):
            return {"ok": True, "started": False, "socket": str(socket_path)}

        worker_main = root / "worker_main.py"
        if not worker_main.exists():
            raise FileNotFoundError(f"missing worker entrypoint: {worker_main}")
        log_path = logs_dir / "worker.log"
        env = os.environ.copy()
        env["WORKBOT_HOME"] = str(root)
        with log_path.open("ab", buffering=0) as logf:
            proc = subprocess.Popen(
                [sys.executable, str(worker_main), "daemon"],
                cwd=str(root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        (run_dir / "worker.pid").write_text(str(proc.pid), encoding="utf-8")

        while time.monotonic() < deadline:
            if socket_alive(socket_path):
                return {
                    "ok": True,
                    "started": True,
                    "pid": proc.pid,
                    "socket": str(socket_path),
                    "log": str(log_path),
                }
            rc = proc.poll()
            if rc is not None:
                tail = ""
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
                except OSError:
                    pass
                raise RuntimeError(f"worker daemon exited during startup rc={rc}: {tail}")
            time.sleep(0.1)
        raise RuntimeError(f"worker socket did not become ready within {wait_seconds}s: {socket_path}")
    finally:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        finally:
            lockf.close()


def stop_daemon(root: Path, wait_seconds: float = 5.0) -> dict:
    """Stop the detached worker started by :func:`ensure_daemon`.

    The PID file is only trusted when /proc confirms that the process command
    line belongs to this WorkBot worker. This avoids killing an unrelated
    process if a stale PID was reused after a reboot.
    """
    root = root.expanduser().resolve()
    run_dir = root / "run"
    pid_path = run_dir / "worker.pid"
    socket_path = run_dir / "worker.sock"
    if not pid_path.exists():
        if not socket_alive(socket_path):
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
            return {"ok": True, "stopped": False, "reason": "not-running"}
        raise RuntimeError(f"worker socket is alive but PID file is missing: {socket_path}")

    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except Exception as exc:
        raise RuntimeError(f"invalid worker PID file: {pid_path}") from exc

    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        pid_path.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)
        return {"ok": True, "stopped": False, "reason": "stale-pid", "pid": pid}

    cmdline = ""
    try:
        cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError:
        pass
    if "worker_main.py" not in cmdline or "daemon" not in cmdline:
        raise RuntimeError(f"refusing to stop pid {pid}: command does not look like WorkBot worker: {cmdline!r}")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline and Path(f"/proc/{pid}").exists():
            time.sleep(0.1)

    if Path(f"/proc/{pid}").exists():
        raise RuntimeError(f"worker pid {pid} did not stop")
    pid_path.unlink(missing_ok=True)
    socket_path.unlink(missing_ok=True)
    return {"ok": True, "stopped": True, "pid": pid}
