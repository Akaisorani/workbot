from __future__ import annotations

import asyncio
import json
from workbot.storage.sqlite import Store
from workbot.transport.protocol import Message, new_id, request


_TERMINAL = {"completed", "failed", "cancelled"}
_ACTIVE = {"created", "running", "cancelling"}


class TaskManager:
    def __init__(self, store: Store, ssh_manager=None):
        self.store = store
        self.ssh = ssh_manager
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._terminal_waiters: dict[str, list[asyncio.Future]] = {}

    def bind_transport(self, ssh_manager) -> None:
        self.ssh = ssh_manager

    async def _request(self, node: str, msg: Message, timeout: float = 20) -> Message:
        if not self.ssh:
            raise RuntimeError("SSH manager not bound")
        fut = asyncio.get_running_loop().create_future()
        self._pending_requests[msg.id] = fut
        try:
            await self.ssh.send(node, msg)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_requests.pop(msg.id, None)

    async def request_node(self, node: str, method: str, data: dict | None = None, *, timeout: float = 30) -> dict:
        """Send a bounded control-plane RPC to a connected worker and return result data."""
        msg = request(method, source="workbot", target=node, data=data or {})
        resp = await self._request(node, msg, timeout=timeout)
        if not resp.data.get("accepted", True):
            raise RuntimeError(resp.data.get("error", f"remote worker rejected {method}"))
        if "result" in resp.data:
            return dict(resp.data.get("result") or {})
        return dict(resp.data or {})

    async def create_remote_task(self, *, node: str, conversation_id: str | None,
                                 origin_message_id: str | None, title: str,
                                 instruction: str, parent_task_id: str | None = None,
                                 workflow_id: str | None = None,
                                 workflow_step_id: str | None = None,
                                 notify_mode: str = "direct",
                                 requested_by: str | None = None,
                                 authorized_by: str | None = None,
                                 authorization_message_id: str | None = None) -> str:
        task_id = new_id("task")
        self.store.execute(
            """
            INSERT INTO tasks(task_id, origin_conversation_id, origin_message_id,
                              requested_by, authorized_by, authorization_message_id, parent_task_id,
                              node, task_type, state, title, instruction,
                              workflow_id, workflow_step_id, notify_mode, current_turn, active_instruction_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'codeagent', 'created', ?, ?, ?, ?, ?, 1, 1)
            """,
            (task_id, conversation_id, origin_message_id, requested_by, authorized_by, authorization_message_id,
             parent_task_id, node, title, instruction, workflow_id, workflow_step_id, notify_mode),
        )
        self.store.execute(
            "INSERT INTO task_instructions(task_id,sequence,mode,instruction,state) VALUES (?,1,'initial',?,'running')",
            (task_id, instruction),
        )
        msg = request("task.create", task_id=task_id, source="workbot", target=node, data={
            "task_type": "codeagent",
            "title": title,
            "instruction": instruction,
            "conversation_id": conversation_id,
            "requested_by": requested_by,
            "authorized_by": authorized_by,
        })
        try:
            resp = await self._request(node, msg)
        except Exception:
            self.store.execute(
                "UPDATE tasks SET state='failed', result_json=?, updated_at=unixepoch() WHERE task_id=?",
                (json.dumps({"error": "task.create transport failed"}, ensure_ascii=False), task_id),
            )
            raise
        if not resp.data.get("accepted"):
            self.store.execute("UPDATE tasks SET state='failed', updated_at=unixepoch() WHERE task_id=?", (task_id,))
            raise RuntimeError(resp.data.get("error", "remote worker rejected task"))
        agent_session_id = str(resp.data.get("agent_session_id") or "") or None
        self.store.execute(
            "UPDATE tasks SET state='running', agent_session_id=COALESCE(?,agent_session_id), updated_at=unixepoch() WHERE task_id=?",
            (agent_session_id, task_id),
        )
        return task_id

    async def add_instruction(self, task_id: str, instruction: str, *, mode: str = "append") -> dict:
        """Queue or steer a new instruction within the same remote Task/CodeAgent session.

        ``append`` waits for the current CodeAgent turn to finish. ``steer`` asks
        the worker to terminate the current managed execution scope and resume
        the same CodeAgent session immediately with the new instruction.
        """
        mode = str(mode or "append").lower()
        if mode not in {"append", "steer"}:
            raise ValueError("mode must be append or steer")
        instruction = str(instruction or "").strip()
        if not instruction:
            raise ValueError("instruction is empty")
        row = self.get(task_id)
        if not row:
            raise KeyError(f"unknown task {task_id}")
        if row["state"] in _TERMINAL:
            raise RuntimeError(f"{task_id} 已结束（{row['state']}）；请使用 /continue 创建后续任务。")
        if row["state"] == "cancelling":
            raise RuntimeError(f"{task_id} 正在取消，不能追加指令")
        msg = request("task.instruction", task_id=task_id, source="workbot", target=row["node"], data={
            "mode": mode,
            "instruction": instruction,
        })
        resp = await self._request(row["node"], msg, timeout=30)
        if not resp.data.get("accepted"):
            raise RuntimeError(resp.data.get("error", "remote worker rejected task instruction"))
        seq = int(resp.data.get("sequence") or 0)
        state = "running" if mode == "steer" else "queued"
        if seq:
            self.store.execute(
                "INSERT OR REPLACE INTO task_instructions(task_id,sequence,mode,instruction,state,started_at) "
                "VALUES (?,?,?,?,?,CASE WHEN ?='running' THEN unixepoch() ELSE NULL END)",
                (task_id, seq, mode, instruction, state, state),
            )
        if resp.data.get("agent_session_id"):
            self.store.execute(
                "UPDATE tasks SET agent_session_id=?, current_turn=MAX(current_turn,?), active_instruction_seq=COALESCE(?,active_instruction_seq), updated_at=unixepoch() WHERE task_id=?",
                (str(resp.data["agent_session_id"]), int(resp.data.get("turn") or 0), seq if mode == "steer" else None, task_id),
            )
        return dict(resp.data)

    async def steer(self, task_id: str, instruction: str) -> dict:
        return await self.add_instruction(task_id, instruction, mode="steer")

    async def append(self, task_id: str, instruction: str) -> dict:
        return await self.add_instruction(task_id, instruction, mode="append")

    def instructions(self, task_id: str) -> list:
        return self.store.query_all(
            "SELECT * FROM task_instructions WHERE task_id=? ORDER BY sequence", (task_id,)
        )

    async def cancel(self, task_id: str) -> dict:
        row = self.get(task_id)
        if not row:
            raise KeyError(f"unknown task {task_id}")
        if row["state"] in _TERMINAL:
            return {"accepted": True, "already_terminal": True, "state": row["state"]}
        self.store.execute("UPDATE tasks SET state='cancelling', updated_at=unixepoch() WHERE task_id=?", (task_id,))
        msg = request("task.cancel", task_id=task_id, source="workbot", target=row["node"])
        resp = await self._request(row["node"], msg)
        if not resp.data.get("accepted"):
            # Do not leave a false cancelling state if the worker rejected it.
            self.store.execute("UPDATE tasks SET state='running', updated_at=unixepoch() WHERE task_id=?", (task_id,))
            raise RuntimeError(resp.data.get("error", "remote worker rejected cancellation"))
        return dict(resp.data)

    async def status_remote(self, task_id: str) -> dict | None:
        row = self.get(task_id)
        if not row:
            return None
        msg = request("task.status", task_id=task_id, source="workbot", target=row["node"])
        resp = await self._request(row["node"], msg)
        return resp.data.get("task")

    async def retry(self, task_id: str, *, conversation_id: str | None = None,
                    origin_message_id: str | None = None, extra_instruction: str = "") -> str:
        row = self.get(task_id)
        if not row:
            raise KeyError(f"unknown task {task_id}")
        instruction = row["instruction"] or ""
        if extra_instruction.strip():
            instruction += "\n\n--- Retry / continuation instruction ---\n" + extra_instruction.strip()
        return await self.create_remote_task(
            node=row["node"],
            conversation_id=conversation_id or row["origin_conversation_id"],
            origin_message_id=origin_message_id,
            title=f"继续：{row['title'] or task_id}",
            instruction=instruction,
            parent_task_id=task_id,
        )

    async def continue_from(self, task_id: str, instruction: str, *, conversation_id: str | None = None,
                            origin_message_id: str | None = None) -> str:
        row = self.get(task_id)
        if not row:
            raise KeyError(f"unknown task {task_id}")
        previous = self.store.loads(row["result_json"], {})
        previous_summary = str(previous.get("summary") or previous.get("error") or "")[-5000:]
        prompt = (
            "Continue work related to the previous WorkBot task. Preserve the user's intent and use the previous "
            "result only as context; decide the necessary actions yourself.\n\n"
            f"--- Previous task instruction ---\n{row['instruction'] or ''}\n"
            f"--- Previous task result ---\n{previous_summary}\n"
            f"--- New user instruction ---\n{instruction.strip()}"
        )
        return await self.create_remote_task(
            node=row["node"], conversation_id=conversation_id or row["origin_conversation_id"],
            origin_message_id=origin_message_id, title=f"继续：{row['title'] or task_id}",
            instruction=prompt, parent_task_id=task_id,
        )

    def handle_response(self, msg: Message) -> bool:
        if not msg.correlation_id:
            return False
        fut = self._pending_requests.get(msg.correlation_id)
        if fut and not fut.done():
            fut.set_result(msg)
            return True
        return False

    def handle_event(self, node: str, msg: Message) -> bool:
        # A cancelled/completed/failed task is authoritative. Late background
        # timers or detached descendants may still emit progress/notification
        # events; never route them and never allow a conflicting late terminal
        # event to rewrite the task state.
        existing = self.get(msg.task_id) if msg.task_id else None
        already_terminal = bool(existing and existing["state"] in _TERMINAL)
        cur = self.store.execute(
            """
            INSERT OR IGNORE INTO events(event_id, node, task_id, event_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (msg.id, node, msg.task_id, msg.event or "unknown", json.dumps(msg.data, ensure_ascii=False)),
        )
        is_new = cur.rowcount > 0
        if already_terminal:
            return False
        if not msg.task_id:
            return is_new
        if msg.event in {"task.turn.started", "task.turn.queued", "task.turn.completed", "task.turn.superseded", "task.turn.failed"}:
            seq = int(msg.data.get("sequence") or 0)
            if seq:
                state_map = {
                    "task.turn.started": "running",
                    "task.turn.queued": "queued",
                    "task.turn.completed": "completed",
                    "task.turn.superseded": "superseded",
                    "task.turn.failed": "failed",
                }
                state_i = state_map[msg.event]
                self.store.execute(
                    "UPDATE task_instructions SET state=?, started_at=CASE WHEN ?='running' THEN COALESCE(started_at,unixepoch()) ELSE started_at END, "
                    "completed_at=CASE WHEN ? IN ('completed','superseded','failed') THEN unixepoch() ELSE completed_at END "
                    "WHERE task_id=? AND sequence=?",
                    (state_i, state_i, state_i, msg.task_id, seq),
                )
                if msg.event == "task.turn.started":
                    self.store.execute(
                        "UPDATE tasks SET state='running', current_turn=MAX(current_turn,?), active_instruction_seq=?, "
                        "agent_session_id=COALESCE(?,agent_session_id), updated_at=unixepoch() WHERE task_id=?",
                        (int(msg.data.get("turn") or 0), seq, msg.data.get("agent_session_id"), msg.task_id),
                    )
            return is_new
        if msg.event == "task.started":
            state = "running"
        elif msg.event == "task.completed":
            state = "completed"
        elif msg.event == "task.failed":
            state = "failed"
        elif msg.event == "task.cancelled":
            state = "cancelled"
        else:
            return is_new
        self.store.execute(
            "UPDATE tasks SET state=?, result_json=?, updated_at=unixepoch() WHERE task_id=?",
            (state, json.dumps(msg.data, ensure_ascii=False), msg.task_id),
        )
        if state in _TERMINAL:
            waiters = self._terminal_waiters.pop(msg.task_id, [])
            row = self.get(msg.task_id)
            for fut in waiters:
                if not fut.done():
                    fut.set_result(row)
        return is_new

    async def wait_for_terminal(self, task_id: str, timeout: float = 3600):
        row = self.get(task_id)
        if row and row["state"] in _TERMINAL:
            return row
        fut = asyncio.get_running_loop().create_future()
        self._terminal_waiters.setdefault(task_id, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            waiters = self._terminal_waiters.get(task_id)
            if waiters and fut in waiters:
                waiters.remove(fut)
                if not waiters:
                    self._terminal_waiters.pop(task_id, None)

    def get(self, task_id: str):
        return self.store.query_one("SELECT * FROM tasks WHERE task_id=?", (task_id,))

    def latest_for_conversation(self, conversation_id: str, *, active_only: bool = False):
        if active_only:
            return self.store.query_one(
                "SELECT * FROM tasks WHERE origin_conversation_id=? AND state IN ('created','running','cancelling') "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            )
        return self.store.query_one(
            "SELECT * FROM tasks WHERE origin_conversation_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        )

    def active_for_conversation(self, conversation_id: str) -> list:
        return self.store.query_all(
            "SELECT * FROM tasks WHERE origin_conversation_id=? AND state IN ('created','running','cancelling') "
            "ORDER BY created_at DESC",
            (conversation_id,),
        )
