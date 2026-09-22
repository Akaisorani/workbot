from __future__ import annotations

import json
from workbot.storage.sqlite import Store
from workbot.transport.protocol import new_id
from .models import WorkflowPlan, WorkflowStep


_TERMINAL = {"completed", "failed", "cancelled"}
_ACTIVE = {"created", "running", "recovering", "blocked", "cancelling"}


class WorkflowManager:
    def __init__(self, store: Store):
        self.store = store

    def create(self, *, conversation_id: str, origin_message_id: str | None,
               instruction: str, plan: WorkflowPlan,
               requested_by: str | None = None, authorized_by: str | None = None,
               authorization_message_id: str | None = None) -> str:
        workflow_id = new_id("wf")
        self.store.execute(
            """
            INSERT INTO workflows(workflow_id, origin_conversation_id, origin_message_id,
                                  requested_by, authorized_by, authorization_message_id,
                                  state, summary, original_instruction)
            VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
            """,
            (workflow_id, conversation_id, origin_message_id, requested_by, authorized_by,
             authorization_message_id, plan.summary, instruction),
        )
        for ordinal, step in enumerate(plan.steps, 1):
            self.store.execute(
                """
                INSERT INTO workflow_steps(workflow_id, step_id, ordinal, executor, node,
                                           instruction, depends_on_json, state,
                                           notify_on_complete, milestone)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    workflow_id, step.step_id, ordinal, step.executor, step.node,
                    step.instruction, json.dumps(step.depends_on, ensure_ascii=False),
                    1 if step.notify_on_complete else 0, step.milestone,
                ),
            )
        return workflow_id

    def set_workflow_state(self, workflow_id: str, state: str, result: dict | None = None,
                           recovery_reason: str | None = None) -> None:
        self.store.execute(
            "UPDATE workflows SET state=?, result_json=?, recovery_reason=?, updated_at=unixepoch() WHERE workflow_id=?",
            (state, json.dumps(result, ensure_ascii=False) if result is not None else None,
             recovery_reason, workflow_id),
        )

    def set_step_state(self, workflow_id: str, step_id: str, state: str, *,
                       result_text: str | None = None, task_id: str | None = None,
                       increment_attempt: bool = False) -> None:
        attempt_expr = "attempts=attempts+1," if increment_attempt else ""
        self.store.execute(
            f"""
            UPDATE workflow_steps SET state=?,
                result_text=COALESCE(?, result_text),
                task_id=COALESCE(?, task_id), {attempt_expr} updated_at=unixepoch()
            WHERE workflow_id=? AND step_id=?
            """,
            (state, result_text, task_id, workflow_id, step_id),
        )

    def reset_step(self, workflow_id: str, step_id: str, *, clear_task: bool = True) -> None:
        if clear_task:
            self.store.execute(
                "UPDATE workflow_steps SET state='pending', result_text=NULL, task_id=NULL, updated_at=unixepoch() "
                "WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            )
        else:
            self.store.execute(
                "UPDATE workflow_steps SET state='pending', result_text=NULL, updated_at=unixepoch() "
                "WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            )

    def mark_interrupted_windows_steps(self, workflow_id: str) -> list[str]:
        rows = self.store.query_all(
            "SELECT step_id FROM workflow_steps WHERE workflow_id=? AND executor='windows' AND state='running'",
            (workflow_id,),
        )
        ids = [r["step_id"] for r in rows]
        for sid in ids:
            self.store.execute(
                "UPDATE workflow_steps SET state='interrupted', updated_at=unixepoch() WHERE workflow_id=? AND step_id=?",
                (workflow_id, sid),
            )
        return ids

    def plan_from_store(self, workflow_id: str) -> WorkflowPlan:
        wf = self.get(workflow_id)
        if not wf:
            raise KeyError(workflow_id)
        steps = [
            WorkflowStep(
                step_id=s["step_id"], executor=s["executor"], node=s["node"],
                instruction=s["instruction"], depends_on=s["depends_on"],
                notify_on_complete=s["notify_on_complete"], milestone=s["milestone"] or "",
            )
            for s in self.steps(workflow_id)
        ]
        return WorkflowPlan(summary=wf["summary"] or workflow_id, steps=steps)

    def get(self, workflow_id: str):
        return self.store.query_one("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,))

    def steps(self, workflow_id: str) -> list[dict]:
        rows = self.store.query_all(
            "SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY ordinal", (workflow_id,)
        )
        values = []
        for row in rows:
            item = dict(row)
            item["depends_on"] = json.loads(item.pop("depends_on_json") or "[]")
            item["notify_on_complete"] = bool(item["notify_on_complete"])
            values.append(item)
        return values

    def step(self, workflow_id: str, step_id: str):
        rows = self.steps(workflow_id)
        return next((s for s in rows if s["step_id"] == step_id), None)

    def latest_for_conversation(self, conversation_id: str, *, active_only: bool = False):
        if active_only:
            placeholders = ",".join("?" for _ in _ACTIVE)
            return self.store.query_one(
                f"SELECT * FROM workflows WHERE origin_conversation_id=? AND state IN ({placeholders}) "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id, *_ACTIVE),
            )
        return self.store.query_one(
            "SELECT * FROM workflows WHERE origin_conversation_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        )

    def active(self) -> list:
        placeholders = ",".join("?" for _ in _ACTIVE)
        return self.store.query_all(
            f"SELECT * FROM workflows WHERE state IN ({placeholders}) ORDER BY created_at",
            tuple(_ACTIVE),
        )

    def active_for_conversation(self, conversation_id: str) -> list:
        placeholders = ",".join("?" for _ in _ACTIVE)
        return self.store.query_all(
            f"SELECT * FROM workflows WHERE origin_conversation_id=? AND state IN ({placeholders}) ORDER BY created_at DESC",
            (conversation_id, *_ACTIVE),
        )
