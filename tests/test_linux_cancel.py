import json
import pytest

from linux.workbot_worker.daemon import WorkerDaemon
from linux.workbot_worker.protocol import Message


class Writer:
    def __init__(self): self.buf = b""
    def write(self, data): self.buf += data
    async def drain(self): pass


@pytest.mark.asyncio
async def test_linux_worker_cancel_without_session(tmp_path):
    root = tmp_path / "worker"
    d = WorkerDaemon(root)
    d.store.execute(
        "INSERT INTO tasks(task_id,task_type,title,instruction,state) VALUES (?,?,?,?,?)",
        ("task-abc", "codeagent", "x", "x", "running"),
    )
    w = Writer()
    req = Message(type="request", method="task.cancel", task_id="task-abc", source="workbot")
    await d._cancel_task(req, w)
    assert d.store.one("SELECT state FROM tasks WHERE task_id='task-abc'")["state"] == "cancelled"
    pending = d.store.pending()
    assert len(pending) == 1
    assert 'task.cancelled' in pending[0]["payload_json"]
    assert b'"accepted":true' in w.buf
