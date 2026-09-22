from __future__ import annotations

import json
import time
from pathlib import Path

from .shared_gate import SharedDirectoryLock


class SharedRateBudget:
    """Small JSON-backed sliding-window limiter shared by WorkBot/gateway."""

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)
        self.lock = SharedDirectoryLock(str(self.state_path) + ".lock", timeout=10, stale_after=30)

    def _load(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save(self, value: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def try_reserve(self, key: str, *, limit: int, window_seconds: float, min_interval_seconds: float) -> tuple[bool, float, int]:
        now = time.time()
        limit = max(1, int(limit))
        window = max(1.0, float(window_seconds))
        spacing = max(0.0, float(min_interval_seconds))
        with self.lock:
            state = self._load()
            entries = [float(x) for x in state.get(key, []) if now - float(x) < window]
            wait = 0.0
            if entries:
                wait = max(wait, entries[-1] + spacing - now)
            if len(entries) >= limit:
                wait = max(wait, entries[0] + window - now)
            if wait > 0:
                state[key] = entries
                self._save(state)
                return False, wait, len(entries)
            entries.append(now)
            state[key] = entries
            self._save(state)
            return True, 0.0, len(entries)

    def reserve_blocking(self, key: str, *, limit: int, window_seconds: float, min_interval_seconds: float, timeout: float = 120.0) -> float:
        started = time.monotonic()
        total_wait = 0.0
        while True:
            ok, wait, _ = self.try_reserve(
                key, limit=limit, window_seconds=window_seconds, min_interval_seconds=min_interval_seconds
            )
            if ok:
                return total_wait
            if time.monotonic() - started + wait > timeout:
                raise TimeoutError(f"rate budget wait timed out for {key}")
            sleep_for = max(0.05, min(wait, 2.0))
            time.sleep(sleep_for)
            total_wait += sleep_for
