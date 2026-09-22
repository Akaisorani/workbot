import asyncio
from pathlib import Path

import pytest

from workbot.storage.sqlite import Store
from workbot.tasks.manager import TaskManager
from workbot.transport.protocol import Message
from workbot.im.welink import WeLinkAdapter


class SteeringSSH:
    def __init__(self, manager):
        self.manager = manager
        self.sent = []

    async def send(self, node, msg):
        self.sent.append((node, msg))
        if msg.method == "task.instruction":
            self.manager.handle_response(Message(
                type="response", correlation_id=msg.id, task_id=msg.task_id,
                data={
                    "accepted": True,
                    "mode": msg.data["mode"],
                    "sequence": 2,
                    "turn": 2 if msg.data["mode"] == "steer" else 1,
                    "agent_session_id": "11111111-1111-4111-8111-111111111111",
                    "state": "running" if msg.data["mode"] == "steer" else "queued",
                },
            ))


@pytest.mark.asyncio
async def test_task_manager_steer_keeps_same_task_and_records_instruction(tmp_path: Path):
    store = Store(tmp_path / "w.db")
    tm = TaskManager(store)
    ssh = SteeringSSH(tm)
    tm.bind_transport(ssh)
    store.execute(
        "INSERT INTO tasks(task_id,origin_conversation_id,node,task_type,state,title,instruction,current_turn,active_instruction_seq) "
        "VALUES ('task-a','c','linux-server1','codeagent','running','fastcheck','release',1,1)"
    )
    store.execute(
        "INSERT INTO task_instructions(task_id,sequence,mode,instruction,state) VALUES ('task-a',1,'initial','release','running')"
    )
    result = await tm.steer("task-a", "stop release; use debug")
    assert result["sequence"] == 2
    assert ssh.sent[-1][1].method == "task.instruction"
    assert ssh.sent[-1][1].data["mode"] == "steer"
    rows = tm.instructions("task-a")
    assert [r["sequence"] for r in rows] == [1, 2]
    assert rows[-1]["instruction"] == "stop release; use debug"
    task = tm.get("task-a")
    assert task["current_turn"] == 2
    assert task["agent_session_id"] == "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_worker_steer_supersedes_old_turn_and_resumes_same_session(tmp_path: Path):
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "linux"))
    from workbot_worker.daemon import WorkerDaemon
    from workbot_worker.protocol import Message as WorkerMessage

    d = WorkerDaemon(tmp_path)
    task_id = "task-steer123"
    sid = "22222222-2222-4222-8222-222222222222"
    d.store.execute(
        "INSERT INTO tasks(task_id,task_type,title,instruction,conversation_id,state,agent_session_id,runtime_version,current_turn,active_instruction_seq,workdir,session_id) "
        "VALUES (?,?,?,?,?,'running',?,2,1,1,?,?)",
        (task_id, "codeagent", "x", "release", "c", sid, str(tmp_path / "tasks" / task_id), "wb-old-t0001"),
    )
    d.store.execute(
        "INSERT INTO task_instructions(task_id,sequence,mode,instruction,state) VALUES (?,1,'initial','release','running')",
        (task_id,),
    )
    terminated = []
    launched = []
    replies = []

    async def terminate(tid, row, turn=None):
        terminated.append((tid, turn))

    async def launch(tid, seq, turn, mode, instruction, resume):
        launched.append((tid, seq, turn, mode, instruction, resume, d.store.one("SELECT agent_session_id FROM tasks WHERE task_id=?", (tid,))["agent_session_id"]))
        d.store.execute("UPDATE tasks SET state='running',session_id=? WHERE task_id=?", (f"wb-{tid}-t{turn}", tid))
        d.store.execute("UPDATE task_instructions SET state='running' WHERE task_id=? AND sequence=?", (tid, seq))

    async def write(_writer, msg):
        replies.append(msg)

    d._terminate_execution_scope = terminate
    d._launch_turn_locked = launch
    d._write = write
    req = WorkerMessage(
        type="request", id="req-1", method="task.instruction", task_id=task_id,
        data={"mode": "steer", "instruction": "use debug mode"},
    )
    await d._task_instruction(req, object())
    assert terminated == [(task_id, 1)]
    assert launched[0][:6] == (task_id, 2, 2, "steer", "use debug mode", True)
    assert launched[0][6] == sid
    old = d.store.instruction(task_id, 1)
    new = d.store.instruction(task_id, 2)
    assert old["state"] == "superseded"
    assert new["mode"] == "steer"
    row = d.store.one("SELECT current_turn,active_instruction_seq,agent_session_id FROM tasks WHERE task_id=?", (task_id,))
    assert row["current_turn"] == 2 and row["active_instruction_seq"] == 2 and row["agent_session_id"] == sid
    assert replies[-1].data["accepted"] is True


@pytest.mark.asyncio
async def test_worker_append_queues_without_interrupting_current_turn(tmp_path: Path):
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "linux"))
    from workbot_worker.daemon import WorkerDaemon
    from workbot_worker.protocol import Message as WorkerMessage

    d = WorkerDaemon(tmp_path)
    d.store.execute(
        "INSERT INTO tasks(task_id,task_type,state,agent_session_id,runtime_version,current_turn,active_instruction_seq) "
        "VALUES ('task-add','codeagent','running','33333333-3333-4333-8333-333333333333',2,1,1)"
    )
    d.store.execute("INSERT INTO task_instructions(task_id,sequence,mode,instruction,state) VALUES ('task-add',1,'initial','run','running')")
    replies = []
    async def write(_writer, msg): replies.append(msg)
    d._write = write
    req = WorkerMessage(type="request", id="r", method="task.instruction", task_id="task-add", data={"mode":"append","instruction":"analyze failures after finish"})
    await d._task_instruction(req, object())
    row = d.store.instruction("task-add", 2)
    assert row["state"] == "queued" and row["mode"] == "append"
    task = d.store.one("SELECT current_turn,active_instruction_seq FROM tasks WHERE task_id='task-add'")
    assert task["current_turn"] == 1 and task["active_instruction_seq"] == 1
    assert replies[-1].data["state"] == "queued"


@pytest.mark.asyncio
async def test_worker_drops_notification_from_superseded_turn(tmp_path: Path):
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "linux"))
    from workbot_worker.daemon import WorkerDaemon
    from workbot_worker.protocol import Message as WorkerMessage

    d = WorkerDaemon(tmp_path)
    d.store.execute("INSERT INTO tasks(task_id,task_type,state,runtime_version,current_turn) VALUES ('task-x','codeagent','running',2,2)")
    replies = []
    async def write(_writer, msg): replies.append(msg)
    d._write = write
    msg = WorkerMessage(type="event", id="evt-old", event="task.progress", task_id="task-x", data={"summary":"old", "_task_turn":1})
    await d._handle_local(msg, object())
    assert replies[-1].data["dropped"] is True
    assert replies[-1].data["reason"] == "task-turn-superseded"
    assert d.store.pending() == []


def test_welink_history_budget_uses_safety_margin_under_documented_20_per_minute():
    a = WeLinkAdapter("welink-cli", [], discovery={"history_limit_per_minute":20, "history_budget_per_minute":18})
    assert a._history_limit_per_minute == 20
    assert a._history_budget_per_minute == 18
    assert a._history_min_interval_seconds >= 60/18
    now = 1000.0
    a._history_query_times.extend([now - 30 + i for i in range(18)])
    a._next_history_query_at = 0
    assert a._history_slot_available(now) is False
    assert a._history_budget_reset_in(now) > 0

@pytest.mark.asyncio
async def test_windows_workflow_step_steer_cancels_only_inner_agent_and_resumes_same_step(tmp_path: Path):
    import json
    from workbot.main import WorkBot
    from workbot.agents.codeagent import AgentResult
    from workbot.orchestration.models import WorkflowPlan, WorkflowStep

    (tmp_path / "config").mkdir()
    cfg = {
        "database":"state/workbot.db",
        "im":{"cli":"welink-cli","groups":[]},
        "agent":{"command":"codeagent"},
        "nodes":{},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    plan = WorkflowPlan("local", [WorkflowStep("step-1", "windows", "long local work")])
    wf = bot.workflows.create(conversation_id="c", origin_message_id="m", instruction="x", plan=plan)
    bot.workflows.set_step_state(wf, "step-1", "running")

    started = asyncio.Event()
    async def first():
        started.set()
        await asyncio.Event().wait()

    calls = []
    async def continue_instruction(conversation_id, *, instruction, mode, context=""):
        calls.append((conversation_id, instruction, mode, context))
        return AgentResult(text="steered-local-result", session_id="s")
    bot.agents.continue_task_instruction = continue_instruction

    helper = asyncio.create_task(bot._await_windows_step_agent((wf,"step-1"), "c", first, context=f"workflow={wf}, step=step-1"))
    await started.wait()
    msg = await bot._steer_workflow_step(wf, "step-1", "switch to debug", mode="steer")
    assert "中断" in msg
    result = await asyncio.wait_for(helper, timeout=1)
    assert result.text == "steered-local-result"
    assert calls == [("c", "switch to debug", "steer", f"workflow={wf}, step=step-1")]


@pytest.mark.asyncio
async def test_windows_workflow_step_append_runs_after_current_turn(tmp_path: Path):
    import json
    from workbot.main import WorkBot
    from workbot.agents.codeagent import AgentResult
    from workbot.orchestration.models import WorkflowPlan, WorkflowStep

    (tmp_path / "config").mkdir()
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps({"database":"state/workbot.db","im":{"cli":"welink-cli","groups":[]},"agent":{"command":"codeagent"},"nodes":{}}), encoding="utf-8")
    bot = WorkBot(cp)
    plan = WorkflowPlan("local", [WorkflowStep("step-1", "windows", "x")])
    wf = bot.workflows.create(conversation_id="c", origin_message_id="m", instruction="x", plan=plan)
    bot.workflows.set_step_state(wf, "step-1", "running")
    gate = asyncio.Event()
    async def first():
        await gate.wait()
        return AgentResult(text="first", session_id="s")
    calls=[]
    async def cont(conversation_id, *, instruction, mode, context=""):
        calls.append((instruction,mode))
        return AgentResult(text="after", session_id="s")
    bot.agents.continue_task_instruction=cont
    helper=asyncio.create_task(bot._await_windows_step_agent((wf,"step-1"),"c",first,context="ctx"))
    await asyncio.sleep(0)
    msg=await bot._steer_workflow_step(wf,"step-1","analyze failures",mode="append")
    assert "追加" in msg
    gate.set()
    result=await asyncio.wait_for(helper,timeout=1)
    assert result.text=="after"
    assert calls==[("analyze failures","append")]
