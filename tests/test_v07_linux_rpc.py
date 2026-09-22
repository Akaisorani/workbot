import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from linux.workbot_worker.daemon import WorkerDaemon
from linux.workbot_worker.protocol import Message


class FakeWriter:
    def __init__(self):
        self.messages = []
    def write(self, data):
        self.messages.append(data.decode("utf-8").strip())
    async def drain(self):
        return None
    def close(self):
        pass
    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_local_request_is_forwarded_and_response_routed(tmp_path: Path):
    daemon = WorkerDaemon(tmp_path)
    upstream = FakeWriter()
    local = FakeWriter()
    daemon.upstreams.add(upstream)
    req = Message(type="request", method="windows.fs.read", task_id="task-x", data={"path": "D:/x.txt"})
    await daemon._handle_local(req, local)
    assert len(upstream.messages) == 1
    assert req.id in daemon._local_requests

    resp = Message(type="response", correlation_id=req.id, data={"ok": True, "result": {"content": "hello"}})
    await daemon._handle_upstream(resp, upstream)
    assert req.id not in daemon._local_requests
    assert len(local.messages) == 1
    assert '"ok":true' in local.messages[0]


@pytest.mark.asyncio
async def test_local_request_fails_fast_without_windows_upstream(tmp_path: Path):
    daemon = WorkerDaemon(tmp_path)
    local = FakeWriter()
    req = Message(type="request", method="workbot.query", data={"instruction": "x"})
    await daemon._handle_local(req, local)
    assert len(local.messages) == 1
    assert "WorkBotUnavailable" in local.messages[0]

@pytest.mark.skipif(os.name == "nt", reason="Unix domain sockets are a Linux worker integration concern")
@pytest.mark.asyncio
async def test_unix_socket_rpc_roundtrip(tmp_path: Path):
    daemon = WorkerDaemon(tmp_path)
    daemon.socket_path = tmp_path / "worker.sock"
    server = await asyncio.start_unix_server(daemon.handle_client, path=str(daemon.socket_path))
    try:
        up_r, up_w = await asyncio.open_unix_connection(str(daemon.socket_path))
        up_w.write(b'{"role":"upstream"}\n'); await up_w.drain()
        await up_r.readline()  # hello

        local_r, local_w = await asyncio.open_unix_connection(str(daemon.socket_path))
        local_w.write(b'{"role":"local"}\n'); await local_w.drain()
        await local_r.readline()  # hello

        req = Message(type="request", method="windows.fs.read", data={"path":"D:/x.txt"})
        local_w.write((req.dumps()+"\n").encode()); await local_w.drain()
        forwarded = Message.loads((await up_r.readline()).decode())
        assert forwarded.id == req.id
        assert forwarded.method == "windows.fs.read"

        resp = Message(type="response", correlation_id=req.id, data={"ok":True,"result":{"content":"roundtrip"}})
        up_w.write((resp.dumps()+"\n").encode()); await up_w.drain()
        returned = Message.loads((await local_r.readline()).decode())
        assert returned.correlation_id == req.id
        assert returned.data["result"]["content"] == "roundtrip"

        local_w.close(); up_w.close()
        await local_w.wait_closed(); await up_w.wait_closed()
    finally:
        server.close(); await server.wait_closed()
        daemon.socket_path.unlink(missing_ok=True)
