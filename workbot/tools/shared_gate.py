from __future__ import annotations

import json
import os
import time
from pathlib import Path


class SharedDirectoryLock:
    """Small cross-process lock based on atomic mkdir.

    Works on Windows and POSIX without platform-specific modules.  The lock
    directory includes owner metadata and can recover an obviously stale lock
    left by a crashed process.
    """

    def __init__(self, path: str | Path, *, timeout: float = 120.0, stale_after: float = 180.0, poll: float = 0.05):
        self.path = Path(path)
        self.timeout = float(timeout)
        self.stale_after = float(stale_after)
        self.poll = max(0.01, float(poll))
        self._held = False

    def _break_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if age < self.stale_after:
            return False
        try:
            meta = self.path / "owner.json"
            meta.unlink(missing_ok=True)
            self.path.rmdir()
            return True
        except OSError:
            return False

    def acquire(self) -> float:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        while True:
            try:
                os.mkdir(self.path)
                try:
                    (self.path / "owner.json").write_text(
                        json.dumps({"pid": os.getpid(), "acquired_at": time.time()}), encoding="utf-8"
                    )
                except OSError:
                    pass
                self._held = True
                return time.monotonic() - started
            except FileExistsError:
                self._break_stale()
                if time.monotonic() - started >= self.timeout:
                    raise TimeoutError(f"timed out waiting for shared lock {self.path}")
                time.sleep(self.poll)

    def release(self) -> None:
        if not self._held:
            return
        try:
            (self.path / "owner.json").unlink(missing_ok=True)
            self.path.rmdir()
        except FileNotFoundError:
            pass
        finally:
            self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
