from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from .lifecycle import ensure_daemon


async def relay(root: Path):
    # The SSH relay is only a transport endpoint.  Make the node-local daemon
    # self-healing so an SSH reconnect does not depend on systemd --user/linger.
    await asyncio.to_thread(ensure_daemon, root)
    socket_path = root / "run" / "worker.sock"

    last_error: Exception | None = None
    for _ in range(30):
        try:
            reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=8 * 1024 * 1024)
            break
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last_error = exc
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError(f"worker socket unavailable after startup: {socket_path}: {last_error}")

    writer.write((json.dumps({"role": "upstream"}) + "\n").encode())
    await writer.drain()
    # consume daemon hello; never put it on protocol stdout
    await reader.readline()

    async def stdin_to_socket():
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            writer.write(line)
            await writer.drain()

    async def socket_to_stdout():
        while True:
            line = await reader.readline()
            if not line:
                break
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    tasks = [asyncio.create_task(stdin_to_socket()), asyncio.create_task(socket_to_stdout())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    writer.close()
    await writer.wait_closed()


def main():
    root = Path(os.environ.get("WORKBOT_HOME", str(Path.home() / ".workbot")))
    asyncio.run(relay(root))
