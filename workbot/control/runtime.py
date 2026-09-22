from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from workbot.main import WorkBot
from .service import WorkBotControlService


class GuiLogBuffer(logging.Handler):
    """Thread-safe bounded log buffer for the desktop console."""

    def __init__(self, max_lines: int = 4000):
        super().__init__()
        self._items: deque[tuple[int, str]] = deque(maxlen=max(100, int(max_lines)))
        self._seq = 0
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            text = record.getMessage()
        with self._lock:
            self._seq += 1
            self._items.append((self._seq, text))

    def read_since(self, seq: int = 0, *, limit: int = 1000) -> tuple[int, list[str]]:
        with self._lock:
            rows = [(s, t) for s, t in self._items if s > int(seq)]
            rows = rows[-max(1, int(limit)):]
            newest = self._seq
        return newest, [text for _, text in rows]


class RuntimeController:
    """Own a live WorkBot asyncio runtime behind a thread-safe desktop API."""

    def __init__(self, config_path: str | Path, *, log_buffer: GuiLogBuffer | None = None):
        self.config_path = Path(config_path).resolve()
        self.log_buffer = log_buffer or GuiLogBuffer()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot: WorkBot | None = None
        self._service: WorkBotControlService | None = None
        self._state = "stopped"
        self._error = ""
        self._started_at = 0.0
        self._lock = threading.RLock()
        self._ready = threading.Event()
        root = logging.getLogger()
        if self.log_buffer not in root.handlers:
            root.addHandler(self.log_buffer)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._state = "starting"
            self._error = ""
            self._ready.clear()
            self._thread = threading.Thread(target=self._thread_main, name="workbot-runtime", daemon=True)
            self._thread.start()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        try:
            bot = WorkBot(self.config_path)
            service = WorkBotControlService(bot)
            with self._lock:
                self._bot = bot
                self._service = service
                self._state = "running"
                self._started_at = time.time()
            self._ready.set()
            loop.run_until_complete(bot.run())
            with self._lock:
                if self._state != "error":
                    self._state = "stopped"
        except Exception as exc:
            logging.getLogger(__name__).exception("WorkBot runtime failed")
            with self._lock:
                self._error = str(exc)
                self._state = "error"
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            with self._lock:
                self._loop = None
                self._bot = None
                self._service = None
                if self._state == "stopping":
                    self._state = "stopped"

    def stop(self) -> None:
        with self._lock:
            loop, bot = self._loop, self._bot
            if not loop or not bot:
                self._state = "stopped"
                return
            self._state = "stopping"
        loop.call_soon_threadsafe(bot.stop)

    def wait_stopped(self, timeout: float = 10.0) -> bool:
        thread = self._thread
        if not thread:
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def restart(self) -> None:
        def worker():
            self.stop()
            self.wait_stopped(15.0)
            self.start()
        threading.Thread(target=worker, name="workbot-restart", daemon=True).start()

    def _submit(self, coro, timeout: float = 5.0):
        with self._lock:
            loop = self._loop
            state = self._state
        if not loop or state not in {"running", "stopping"}:
            raise RuntimeError(f"WorkBot runtime is not running (state={state})")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    def snapshot(self, timeout: float = 4.0) -> dict[str, Any]:
        with self._lock:
            service = self._service
            state = self._state
            error = self._error
            started = self._started_at
        base = {
            "controller": {
                "state": state,
                "error": error,
                "started_at": started,
                "uptime": max(0.0, time.time() - started) if started and state in {"running", "stopping"} else 0.0,
                "config_path": str(self.config_path),
            }
        }
        if not service or state not in {"running", "stopping"}:
            return base
        try:
            live = self._submit(service.snapshot(), timeout=timeout)
            base.update(live)
        except Exception as exc:
            base["controller"]["snapshot_error"] = str(exc)
        return base

    def action(self, name: str, timeout: float = 20.0, **kwargs):
        with self._lock:
            service = self._service
        if service is None:
            raise RuntimeError("WorkBot runtime is not running")
        return self._submit(service.action(name, **kwargs), timeout=timeout)
