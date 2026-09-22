import asyncio
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "linux"))
from workbot_worker.daemon import WorkerDaemon


@pytest.mark.asyncio
async def test_remote_tasks_receive_distinct_codeagent_sessions(tmp_path: Path):
    d = WorkerDaemon(tmp_path)

    class Writer:
        def __init__(self): self.buf = bytearray()
        def write(self, b): self.buf.extend(b)
        async def drain(self): pass

    from workbot_worker.protocol import Message
    w = Writer()
    for tid in ("task-aaa111", "task-bbb222"):
        req = Message(type="request", method="task.create", task_id=tid, data={
            "task_type": "codeagent", "title": tid, "instruction": "x", "conversation_id": "c"
        })
        # Avoid launching actual tmux; only test durable creation metadata.
        original = asyncio.create_task
        created = []
        def fake_create(coro, *args, **kwargs):
            created.append(coro)
            class Dummy:
                def done(self): return True
            return Dummy()
        asyncio.create_task = fake_create
        try:
            await d._create_task(req, w)
        finally:
            asyncio.create_task = original
            for coro in created:
                coro.close()
    a = d.store.one("SELECT agent_session_id FROM tasks WHERE task_id='task-aaa111'")["agent_session_id"]
    b = d.store.one("SELECT agent_session_id FROM tasks WHERE task_id='task-bbb222'")["agent_session_id"]
    assert a and b and a != b
    assert re.fullmatch(r"[0-9a-f-]{36}", a)
