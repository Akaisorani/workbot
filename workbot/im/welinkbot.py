from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from workbot.conversation.models import IncomingMessage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class WeLinkBotHealth:
    enabled: bool = False
    connected: bool = False
    authenticated: bool = False
    connections: int = 0
    reconnects: int = 0
    received_frames: int = 0
    received_messages: int = 0
    ignored_frames: int = 0
    dropped_messages: int = 0
    parse_errors: int = 0
    last_event_at: float = 0.0
    last_message_at: float = 0.0
    connected_at: float = 0.0
    disconnected_at: float = 0.0
    last_error: str = ""
    backfill_generation: int = 0


class WeLinkBotReceiver:
    """Realtime receive-only transport for WeLinkBot WebSocket.

    WorkBot deliberately uses this component only as an event source. Sending,
    mail/calendar/OneBox and other actions continue through the managed
    ``welink-cli`` gateway so existing approval, rate-limit and durable-outbox
    behavior remains authoritative.
    """

    def __init__(
        self,
        config: dict,
        normalizer: Callable[[dict], IncomingMessage | None],
    ):
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))
        self.url = str(self.config.get("url", "ws://127.0.0.1:4080"))
        secret_env = str(self.config.get("secret_env", "") or "").strip()
        self.secret = str(os.environ.get(secret_env, self.config.get("secret", "")) if secret_env else self.config.get("secret", ""))
        self.open_timeout = max(1.0, float(self.config.get("open_timeout_seconds", 5)))
        self.auth_timeout = max(1.0, float(self.config.get("auth_timeout_seconds", 5)))
        self.reconnect_initial = max(0.5, float(self.config.get("reconnect_initial_seconds", 1)))
        self.reconnect_max = max(self.reconnect_initial, float(self.config.get("reconnect_max_seconds", 30)))
        self.queue_max = max(100, int(self.config.get("queue_max_messages", 5000)))
        self._normalizer = normalizer
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(maxsize=self.queue_max)
        self.health = WeLinkBotHealth(enabled=self.enabled)
        self._warn_security()

    def _warn_security(self) -> None:
        if not self.enabled:
            return
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower()
        loopback = host in {"127.0.0.1", "localhost", "::1"}
        if not loopback and not self.secret:
            log.warning(
                "WeLinkBot WebSocket is configured without a secret on non-loopback endpoint %s; "
                "this exposes a high-risk local control API",
                self.url,
            )
        elif not self.secret:
            log.warning(
                "WeLinkBot WebSocket has no secret configured; endpoint is loopback-only (%s). "
                "Configure a secret before exposing the service beyond localhost.",
                self.url,
            )

    @property
    def connected(self) -> bool:
        return bool(self.health.connected)

    @property
    def backfill_generation(self) -> int:
        return int(self.health.backfill_generation)

    def diagnostics(self) -> dict:
        now = time.time()
        return {
            "enabled": self.enabled,
            "url": self.url,
            "connected": self.health.connected,
            "authenticated": self.health.authenticated,
            "connections": self.health.connections,
            "reconnects": self.health.reconnects,
            "received_frames": self.health.received_frames,
            "received_messages": self.health.received_messages,
            "ignored_frames": self.health.ignored_frames,
            "dropped_messages": self.health.dropped_messages,
            "parse_errors": self.health.parse_errors,
            "queue_size": self._queue.qsize(),
            "last_event_seconds_ago": None if not self.health.last_event_at else round(max(0.0, now - self.health.last_event_at), 1),
            "last_message_seconds_ago": None if not self.health.last_message_at else round(max(0.0, now - self.health.last_message_at), 1),
            "connected_seconds": 0.0 if not self.health.connected_at or not self.health.connected else round(max(0.0, now - self.health.connected_at), 1),
            "last_error": self.health.last_error[-300:],
            "backfill_generation": self.health.backfill_generation,
            "secret_configured": bool(self.secret),
        }

    @staticmethod
    def _auth_failed(obj: object) -> bool:
        if not isinstance(obj, dict):
            return False
        if obj.get("success") is False:
            return True
        code = obj.get("code", obj.get("status"))
        if code not in (None, 0, "0", 200, "200", "ok", "OK"):
            # Avoid treating arbitrary message types/status strings as failure.
            if isinstance(code, (int, float)) or str(code).isdigit():
                return True
        text = " ".join(str(obj.get(k, "")) for k in ("error", "message", "msg", "reason")).lower()
        return any(x in text for x in ("auth failed", "unauthorized", "forbidden", "invalid secret"))

    def _enqueue(self, msg: IncomingMessage) -> None:
        try:
            self._queue.put_nowait(msg)
            return
        except asyncio.QueueFull:
            # Prefer retaining the newest realtime data. Any dropped gap can be
            # repaired by the CLI history backfill channel.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.health.dropped_messages += 1
            log.warning("WeLinkBot realtime queue full; dropping oldest message for history backfill")
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                self.health.dropped_messages += 1

    async def get_batch(self, *, timeout: float = 0.5, max_items: int = 200) -> list[IncomingMessage]:
        items: list[IncomingMessage] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=max(0.05, timeout))
        except asyncio.TimeoutError:
            return items
        items.append(first)
        while len(items) < max_items:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            return
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - setup installs dependency
            self.health.last_error = "Python package 'websockets' is not installed"
            log.error("WeLinkBot realtime receiver disabled: %s", self.health.last_error)
            return

        delay = self.reconnect_initial
        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    proxy=None,
                    open_timeout=self.open_timeout,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=4 * 1024 * 1024,
                    max_queue=256,
                ) as ws:
                    self.health.connected = True
                    self.health.authenticated = False
                    self.health.connections += 1
                    if self.health.connections > 1:
                        self.health.reconnects += 1
                    self.health.connected_at = time.time()
                    self.health.last_error = ""
                    await ws.send(json.dumps({"type": "auth", "secret": self.secret}, ensure_ascii=False))
                    raw_auth = await asyncio.wait_for(ws.recv(), timeout=self.auth_timeout)
                    try:
                        auth_obj = json.loads(raw_auth.decode("utf-8") if isinstance(raw_auth, bytes) else raw_auth)
                    except Exception:
                        auth_obj = {"raw": str(raw_auth)[:500]}
                    if self._auth_failed(auth_obj):
                        raise RuntimeError(f"WeLinkBot authentication failed: {auth_obj}")
                    self.health.authenticated = True
                    self.health.backfill_generation += 1
                    delay = self.reconnect_initial
                    log.info(
                        "WeLinkBot WebSocket connected url=%s connection=%d backfill_generation=%d",
                        self.url, self.health.connections, self.health.backfill_generation,
                    )

                    async for raw in ws:
                        if stop_event.is_set():
                            break
                        self.health.received_frames += 1
                        self.health.last_event_at = time.time()
                        try:
                            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                            obj = json.loads(text)
                        except Exception as exc:
                            self.health.parse_errors += 1
                            self.health.last_error = f"invalid websocket json: {exc}"
                            log.warning("Ignoring invalid WeLinkBot WebSocket frame: %s", str(raw)[:300])
                            continue
                        try:
                            msg = self._normalizer(obj)
                        except Exception as exc:
                            self.health.parse_errors += 1
                            self.health.last_error = f"message normalization failed: {exc}"
                            log.exception("WeLinkBot message normalization failed")
                            continue
                        if msg is None:
                            self.health.ignored_frames += 1
                            continue
                        self.health.received_messages += 1
                        self.health.last_message_at = time.time()
                        self._enqueue(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.last_error = str(exc)[-500:]
                log.warning("WeLinkBot WebSocket disconnected/error: %s; reconnecting in %.1fs", exc, delay)
            finally:
                if self.health.connected:
                    self.health.disconnected_at = time.time()
                self.health.connected = False
                self.health.authenticated = False

            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(self.reconnect_max, max(self.reconnect_initial, delay * 2))
