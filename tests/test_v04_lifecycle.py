import asyncio
import json
from pathlib import Path

import pytest

from workbot.storage.sqlite import Store
from workbot.tasks.manager import TaskManager
from workbot.transport.protocol import Message, event
from workbot.orchestration.manager import WorkflowManager
from workbot.orchestration.models import WorkflowPlan, WorkflowStep


class FakeSSH:
    def __init__(self, manager):
        self.manager = manager
        self.sent = []

    async def send(self, node, msg):
        self.sent.append((node, msg))
        if msg.type == "request":
            data = {"accepted": True, "state": "cancelled"} if msg.method == "task.cancel" else {"accepted": True}
            self.manager.handle_response(Message(type="response", correlation_id=msg.id, task_id=msg.task_id, data=data))


@pytest.mark.asyncio
async def test_task_cancel_protocol_and_terminal_event(tmp_path):
    store = Store(tmp_path / "w.db")
    manager = TaskManager(store)
    ssh = FakeSSH(manager)
    manager.bind_transport(ssh)
    store.execute(
        "INSERT INTO tasks(task_id,node,task_type,state,title,instruction,notify_mode) VALUES (?,?,?,?,?,?,?)",
        ("task-abc123", "linux-server1", "codeagent", "running", "x", "do x", "direct"),
    )
    result = await manager.cancel("task-abc123")
    assert result["accepted"] is True
    assert ssh.sent[0][1].method == "task.cancel"
    assert manager.get("task-abc123")["state"] == "cancelling"
    manager.handle_event("linux-server1", event("task.cancelled", task_id="task-abc123", data={"status":"cancelled"}))
    assert manager.get("task-abc123")["state"] == "cancelled"


def test_workflow_marks_only_running_windows_step_interrupted(tmp_path):
    store = Store(tmp_path / "w.db")
    wm = WorkflowManager(store)
    plan = WorkflowPlan("x", [
        WorkflowStep("step-1", "windows", "local"),
        WorkflowStep("step-2", "remote", "remote", node="linux-server1"),
    ])
    wf = wm.create(conversation_id="c", origin_message_id="m", instruction="x", plan=plan)
    wm.set_step_state(wf, "step-1", "running")
    wm.set_step_state(wf, "step-2", "running", task_id="task-r")
    interrupted = wm.mark_interrupted_windows_steps(wf)
    assert interrupted == ["step-1"]
    states = {s["step_id"]: s["state"] for s in wm.steps(wf)}
    assert states == {"step-1": "interrupted", "step-2": "running"}


def test_workflow_plan_roundtrip_and_attempts(tmp_path):
    store = Store(tmp_path / "w.db")
    wm = WorkflowManager(store)
    plan = WorkflowPlan("sum", [WorkflowStep("step-1", "remote", "read", node="linux-server1")])
    wf = wm.create(conversation_id="c", origin_message_id=None, instruction="req", plan=plan)
    wm.set_step_state(wf, "step-1", "running", increment_attempt=True)
    restored = wm.plan_from_store(wf)
    assert restored.summary == "sum"
    assert restored.steps[0].node == "linux-server1"
    assert wm.step(wf, "step-1")["attempts"] == 1
