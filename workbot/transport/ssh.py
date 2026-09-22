from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .protocol import Message

log = logging.getLogger(__name__)


class SSHConnection:
    def __init__(self, node: str, ssh_alias: str, relay_command: str,
                 on_message: Callable[[str, Message], Awaitable[None]],
                 on_state_change=None):
        self.node = node
        self.ssh_alias = ssh_alias
        self.relay_command = relay_command
        self.on_message = on_message
        self.on_state_change = on_state_change
        self.proc: asyncio.subprocess.Process | None = None
        self._writer_lock = asyncio.Lock()
        self._stopping = False
        self._runner: asyncio.Task | None = None
        self._connected = asyncio.Event()

    async def start(self) -> None:
        if self._runner and not self._runner.done():
            return
        self._runner = asyncio.create_task(self._run_forever(), name=f"ssh:{self.node}")

    async def stop(self) -> None:
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
        if self._runner:
            await asyncio.gather(self._runner, return_exceptions=True)

    async def send(self, msg: Message) -> None:
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=10)
            except asyncio.TimeoutError as exc:
                raise ConnectionError(f"node {self.node} is not connected") from exc
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise ConnectionError(f"node {self.node} is not connected")
        data = (msg.dumps() + "\n").encode("utf-8")
        async with self._writer_lock:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()

    async def _run_forever(self) -> None:
        backoff = 1
        while not self._stopping:
            try:
                await self._connect_once()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SSH relay %s failed; reconnecting", self.node)
            if not self._stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _connect_once(self) -> None:
        log.info("Connecting SSH relay to %s", self.node)
        self.proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", self.ssh_alias, self.relay_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        assert self.proc.stdout and self.proc.stderr
        self._connected.set()
        if self.on_state_change:
            try:
                self.on_state_change(self.node, True)
            except Exception:
                log.exception("node state callback failed for %s", self.node)
        stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            async for raw in self.proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = Message.loads(line)
                except Exception:
                    log.warning("Ignoring non-protocol output from %s: %r", self.node, line[:300])
                    continue
                await self.on_message(self.node, msg)
            rc = await self.proc.wait()
            raise ConnectionError(f"ssh relay {self.node} exited with {rc}")
        finally:
            self._connected.clear()
            if self.on_state_change:
                try:
                    self.on_state_change(self.node, False)
                except Exception:
                    log.exception("node state callback failed for %s", self.node)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                log.warning("[%s ssh] %s", self.node, text)


class SSHManager:
    def __init__(self, node_configs: dict, on_message, on_state_change=None):
        self.connections = {
            name: SSHConnection(
                name, cfg["ssh_alias"],
                str(cfg.get("relay_command") or "python3 -u ~/.workbot/worker_main.py relay"),
                on_message, on_state_change,
            )
            for name, cfg in node_configs.items() if cfg.get("enabled", True) is not False
        }

    async def start(self) -> None:
        for conn in self.connections.values():
            await conn.start()

    async def stop(self) -> None:
        await asyncio.gather(*(c.stop() for c in self.connections.values()), return_exceptions=True)

    async def send(self, node: str, msg: Message) -> None:
        await self.connections[node].send(msg)


    def is_connected(self, node: str) -> bool:
        conn = self.connections.get(node)
        return bool(conn and conn._connected.is_set() and conn.proc and conn.proc.returncode is None)

    def statuses(self) -> dict[str, bool]:
        return {name: self.is_connected(name) for name in self.connections}
