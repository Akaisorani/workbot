import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbot.agents.manager import AgentManager
from workbot.conversation.manager import ConversationManager
from workbot.orchestration.manager import WorkflowManager
from workbot.orchestration.models import WorkflowPlan, WorkflowStep
from workbot.storage.sqlite import Store
from workbot.tasks.manager import TaskManager
from workbot.transport.protocol import event


class FakeBackend:
    def __init__(self, text):
        self.text = text
    async def run(self, prompt, session_id=None):
        from workbot.agents.codeagent import AgentResult
        return AgentResult(self.text)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_parses_windows_and_remote_dag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text("workspace", encoding="utf-8")
            store = Store(root / "db.sqlite")
            conversations = ConversationManager(store)
            manager = AgentManager({"command": "none"}, root, conversations)
            manager.backend = FakeBackend('''<WORKBOT_PLAN>{
              "summary":"combine local spec and server implementation",
              "steps":[
                {"id":"spec","executor":"windows","node":null,"instruction":"read local spec","depends_on":[],"notify_on_complete":false,"milestone":""},
                {"id":"impl","executor":"remote","node":"linux-server1","instruction":"inspect implementation","depends_on":[],"notify_on_complete":true,"milestone":"服务器检查完成"},
                {"id":"compare","executor":"windows","node":null,"instruction":"compare results","depends_on":["spec","impl"],"notify_on_complete":false,"milestone":""}
              ]
            }</WORKBOT_PLAN>''')
            plan = await manager.plan_workflow("c1", "do it", {"linux-server1": {"ssh_alias": "linux-server1"}})
            self.assertEqual([s.step_id for s in plan.steps], ["spec", "impl", "compare"])
            self.assertEqual(plan.steps[1].node, "linux-server1")
            self.assertEqual(plan.steps[2].depends_on, ["spec", "impl"])

    async def test_task_waiter_resolves_terminal_event(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "db.sqlite")
            tm = TaskManager(store)
            store.execute(
                """INSERT INTO tasks(task_id,node,task_type,state,title,instruction,notify_mode)
                   VALUES ('task-1','linux-server1','codeagent','running','x','x','direct')"""
            )
            waiter = __import__('asyncio').create_task(tm.wait_for_terminal("task-1", timeout=2))
            await __import__('asyncio').sleep(0)
            tm.handle_event("linux-server1", event("task.completed", task_id="task-1", data={"summary": "done"}))
            row = await waiter
            self.assertEqual(row["state"], "completed")
            self.assertIn("done", row["result_json"])

    def test_workflow_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "db.sqlite")
            wm = WorkflowManager(store)
            plan = WorkflowPlan("summary", [
                WorkflowStep("a", "windows", "read"),
                WorkflowStep("b", "remote", "inspect", node="linux-server1", depends_on=["a"], notify_on_complete=True),
            ])
            wid = wm.create(conversation_id="c", origin_message_id="m", instruction="x", plan=plan)
            self.assertEqual(wm.get(wid)["state"], "created")
            steps = wm.steps(wid)
            self.assertEqual(steps[1]["depends_on"], ["a"])
            self.assertTrue(steps[1]["notify_on_complete"])


if __name__ == "__main__":
    unittest.main()
