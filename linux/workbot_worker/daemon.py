from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import shlex
import socket
import uuid
from pathlib import Path

from .protocol import Message, event, response
from .store import WorkerStore
from .workspace import discover_projects as discover_workspace_projects, scan_project as scan_workspace_project

log = logging.getLogger(__name__)


class WorkerDaemon:
    def __init__(self, root: Path):
        self.root = root
        self.run_dir = root / "run"
        self.state_dir = root / "state"
        self.logs_dir = root / "logs"
        for d in (self.run_dir, self.state_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.socket_path = self.run_dir / "worker.sock"
        self.store = WorkerStore(self.state_dir / "worker.db")
        self.upstreams: set[asyncio.StreamWriter] = set()
        self.config = self._load_config()
        self.node_name = str(self.config.get("node_name") or os.environ.get("WORKBOT_NODE_NAME") or socket.gethostname())
        self._lock_file = None
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._task_monitors: dict[str, asyncio.Task] = {}
        # Linux-local synchronous RPC requests are multiplexed through the same
        # persistent SSH upstream. request_id -> (local_writer, upstream_writer, request)
        self._local_requests: dict[str, tuple[asyncio.StreamWriter, asyncio.StreamWriter, Message]] = {}

    def _load_config(self) -> dict:
        p = self.root / "config.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {"codeagent_command": "codeagent", "tmux_command": "tmux"}

    async def serve(self):
        # Prevent a reconnect, installer, or user-systemd instance from starting
        # a second daemon and stealing the Unix socket from the active worker.
        lock_path = self.run_dir / "worker.lock"
        self._lock_file = lock_path.open("a+")
        try:
            import fcntl  # POSIX-only; imported lazily so Windows diagnostics/tests do not need winfcntl.
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError as exc:
            raise RuntimeError("agent-worker daemon requires a POSIX/Linux environment") from exc
        except BlockingIOError as exc:
            raise RuntimeError("another agent-worker instance is already running") from exc
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = await asyncio.start_unix_server(self.handle_client, path=str(self.socket_path), limit=8 * 1024 * 1024)
        os.chmod(self.socket_path, 0o600)
        log.info("agent-worker listening on %s", self.socket_path)
        await self._recover_tasks()
        async with server:
            await server.serve_forever()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        role = "local"
        try:
            first = await reader.readline()
            if not first:
                return
            hello = json.loads(first.decode())
            role = hello.get("role", "local")
            if role == "upstream":
                self.upstreams.add(writer)
            writer.write((json.dumps({"ok": True, "node": self.node_name}) + "\n").encode())
            await writer.drain()
            if role == "upstream":
                await self._send_pending(writer)

            async for raw in reader:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                msg = Message.loads(line)
                if role == "upstream":
                    await self._handle_upstream(msg, writer)
                else:
                    await self._handle_local(msg, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker client failed role=%s", role)
        finally:
            if role == "upstream":
                self.upstreams.discard(writer)
                await self._fail_requests_for_upstream(writer)
            else:
                self._drop_requests_for_local(writer)
            writer.close()
            await writer.wait_closed()

    async def _handle_upstream(self, msg: Message, writer: asyncio.StreamWriter):
        if msg.type == "response" and msg.correlation_id:
            pending = self._local_requests.pop(msg.correlation_id, None)
            if pending:
                local_writer, _, _ = pending
                try:
                    await self._write(local_writer, msg)
                except Exception:
                    log.exception("failed to route upstream RPC response %s to local client", msg.correlation_id)
            return
        if msg.type == "ack":
            ack_id = msg.data.get("event_id") or msg.correlation_id
            if ack_id:
                self.store.ack(str(ack_id))
            return
        if msg.type != "request":
            return
        if msg.method == "ping":
            await self._write(writer, response(msg, {"ok": True, "node": self.node_name}))
            return
        if msg.method == "task.create":
            await self._create_task(msg, writer)
            return
        if msg.method == "task.status":
            row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (msg.task_id,))
            await self._write(writer, response(msg, {"task": dict(row) if row else None}))
            return
        if msg.method == "task.cancel":
            await self._cancel_task(msg, writer)
            return
        if msg.method == "task.instruction":
            await self._task_instruction(msg, writer)
            return
        if msg.method == "workspace.discover":
            roots = self.config.get("workspace_knowledge_roots") or []
            try:
                result = await asyncio.to_thread(discover_workspace_projects, roots, msg.data or {})
                await self._write(writer, response(msg, {"accepted": True, "result": result}))
            except Exception as exc:
                await self._write(writer, response(msg, {"accepted": False, "error": str(exc), "error_type": type(exc).__name__}))
            return
        if msg.method == "workspace.scan":
            roots = self.config.get("workspace_knowledge_roots") or []
            project_root = str((msg.data or {}).get("project_root") or "")
            options = dict((msg.data or {}).get("options") or {})
            known = (msg.data or {}).get("known") or {}
            try:
                result = await asyncio.to_thread(scan_workspace_project, roots, project_root, options, known)
                await self._write(writer, response(msg, {"accepted": True, "result": result}))
            except Exception as exc:
                await self._write(writer, response(msg, {"accepted": False, "error": str(exc), "error_type": type(exc).__name__}))
            return
        await self._write(writer, response(msg, {"accepted": False, "error": f"unknown method {msg.method}"}))

    async def _handle_local(self, msg: Message, writer: asyncio.StreamWriter):
        # Local agent/scripts submit events to a durable outbox.
        if msg.type == "event":
            msg.source = msg.source or self.node_name
            if msg.task_id:
                row = self.store.one("SELECT state,current_turn FROM tasks WHERE task_id=?", (msg.task_id,))
                if row and row["state"] in {"completed", "failed", "cancelled"}:
                    log.info("dropping late event %s for terminal task %s state=%s", msg.event, msg.task_id, row["state"])
                    await self._write(writer, Message(
                        type="ack", correlation_id=msg.id,
                        data={"persisted": False, "dropped": True, "reason": "task-terminal", "event_id": msg.id},
                    ))
                    return
                event_turn = int((msg.data or {}).get("_task_turn") or 0)
                current_turn = int(row["current_turn"] or 0) if row else 0
                if event_turn and current_turn and event_turn != current_turn:
                    log.info("dropping event %s for superseded task turn task=%s event_turn=%s current_turn=%s", msg.event, msg.task_id, event_turn, current_turn)
                    await self._write(writer, Message(
                        type="ack", correlation_id=msg.id,
                        data={"persisted": False, "dropped": True, "reason": "task-turn-superseded", "event_id": msg.id},
                    ))
                    return
            self.store.enqueue(msg)
            await self._broadcast(msg)
            await self._write(writer, Message(type="ack", correlation_id=msg.id, data={"persisted": True, "event_id": msg.id}))
            return
        if msg.type == "request":
            msg.source = msg.source or self.node_name
            if msg.method == "worker.local_status":
                rows = self.store.all(
                    "SELECT task_id,state,session_id,agent_session_id,current_turn,active_instruction_seq,workdir,recovery_count,last_recovered_at,updated_at "
                    "FROM tasks ORDER BY updated_at DESC LIMIT 50"
                )
                await self._write(writer, Message(
                    type="response", correlation_id=msg.id, task_id=msg.task_id,
                    data={"ok": True, "result": {"node": self.node_name, "tasks": [dict(r) for r in rows]}},
                ))
                return
            upstream = next(iter(self.upstreams), None)
            if upstream is None:
                await self._write(writer, Message(
                    type="response", correlation_id=msg.id, task_id=msg.task_id,
                    data={"ok": False, "error": "Windows WorkBot upstream is not connected", "error_type": "WorkBotUnavailable"},
                ))
                return
            self._local_requests[msg.id] = (writer, upstream, msg)
            try:
                await self._write(upstream, msg)
            except Exception:
                self._local_requests.pop(msg.id, None)
                await self._write(writer, Message(
                    type="response", correlation_id=msg.id, task_id=msg.task_id,
                    data={"ok": False, "error": "failed to forward request to Windows WorkBot", "error_type": "TransportError"},
                ))
            return
        await self._write(writer, Message(type="response", correlation_id=msg.id, data={"ok": False, "error": "unsupported local message"}))

    def _drop_requests_for_local(self, local_writer: asyncio.StreamWriter) -> None:
        for req_id, (lw, _, _) in list(self._local_requests.items()):
            if lw is local_writer:
                self._local_requests.pop(req_id, None)

    async def _fail_requests_for_upstream(self, upstream_writer: asyncio.StreamWriter) -> None:
        affected = [
            (req_id, local_writer, req)
            for req_id, (local_writer, uw, req) in list(self._local_requests.items())
            if uw is upstream_writer
        ]
        for req_id, local_writer, req in affected:
            self._local_requests.pop(req_id, None)
            try:
                await self._write(local_writer, Message(
                    type="response", correlation_id=req_id, task_id=req.task_id,
                    data={"ok": False, "error": "Windows WorkBot upstream disconnected during RPC", "error_type": "WorkBotDisconnected"},
                ))
            except Exception:
                pass


    async def _cancel_task(self, req: Message, writer: asyncio.StreamWriter):
        task_id = req.task_id
        if not task_id:
            await self._write(writer, response(req, {"accepted": False, "error": "missing task_id"}))
            return
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if not row:
                await self._write(writer, response(req, {"accepted": False, "error": "unknown task"}))
                return
            if row["state"] in {"completed", "failed", "cancelled"}:
                await self._write(writer, response(req, {"accepted": True, "already_terminal": True, "state": row["state"]}))
                return
            self.store.execute("UPDATE tasks SET state='cancelling',updated_at=unixepoch() WHERE task_id=?", (task_id,))
            await self._terminate_execution_scope(task_id, row)
            seq = int(row["active_instruction_seq"] or 0)
            if seq:
                self.store.execute(
                    "UPDATE task_instructions SET state='cancelled',completed_at=unixepoch() "
                    "WHERE task_id=? AND sequence=? AND state IN ('queued','running')",
                    (task_id, seq),
                )
            payload = {"status": "cancelled", "summary": "Task cancelled by WorkBot", "session": row["session_id"]}
            terminal = self._terminal_message("task.cancelled", task_id, payload)
            self.store.finalize_and_enqueue(task_id, "cancelled", json.dumps(payload, ensure_ascii=False), terminal)
        await self._broadcast(terminal)
        await self._write(writer, response(req, {"accepted": True, "state": "cancelled", "task_id": task_id}))

    async def _create_task(self, req: Message, writer: asyncio.StreamWriter):
        task_id = req.task_id
        if not task_id:
            await self._write(writer, response(req, {"accepted": False, "error": "missing task_id"}))
            return
        data = req.data
        task_type = data.get("task_type", "codeagent")
        title = data.get("title", task_id)
        instruction = data.get("instruction", "")
        conversation_id = data.get("conversation_id")
        existing = self.store.one("SELECT agent_session_id FROM tasks WHERE task_id=?", (task_id,))
        agent_session_id = str(existing["agent_session_id"] or "") if existing else ""
        if not agent_session_id:
            agent_session_id = str(uuid.uuid4())
        self.store.execute(
            "INSERT OR REPLACE INTO tasks(task_id,task_type,title,instruction,conversation_id,state,agent_session_id,"
            "runtime_version,current_turn,active_instruction_seq,updated_at) "
            "VALUES (?,?,?,?,?,'created',?,2,1,1,unixepoch())",
            (task_id, task_type, title, instruction, conversation_id, agent_session_id),
        )
        self.store.execute(
            "INSERT OR REPLACE INTO task_instructions(task_id,sequence,mode,instruction,state) VALUES (?,1,'initial',?,'queued')",
            (task_id, instruction),
        )
        await self._write(writer, response(req, {
            "accepted": True, "task_id": task_id, "agent_session_id": agent_session_id,
            "turn": 1, "sequence": 1,
        }))
        asyncio.create_task(self._run_task(task_id, task_type, instruction), name=f"task:{task_id}")

    async def _task_instruction(self, req: Message, writer: asyncio.StreamWriter):
        task_id = req.task_id
        mode = str((req.data or {}).get("mode") or "append").lower()
        instruction = str((req.data or {}).get("instruction") or "").strip()
        if not task_id or mode not in {"append", "steer"} or not instruction:
            await self._write(writer, response(req, {"accepted": False, "error": "task.instruction requires task_id, mode=append|steer and non-empty instruction"}))
            return
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if not row:
                await self._write(writer, response(req, {"accepted": False, "error": "unknown task"}))
                return
            if row["state"] in {"completed", "failed", "cancelled"}:
                await self._write(writer, response(req, {"accepted": False, "error": f"task already terminal: {row['state']}"}))
                return
            if row["state"] == "cancelling":
                await self._write(writer, response(req, {"accepted": False, "error": "task is cancelling"}))
                return
            seq = self.store.next_instruction_sequence(task_id)
            self.store.execute(
                "INSERT INTO task_instructions(task_id,sequence,mode,instruction,state) VALUES (?,?,?,?, 'queued')",
                (task_id, seq, mode, instruction),
            )
            if mode == "append":
                await self.emit(event("task.turn.queued", task_id=task_id, source=self.node_name, data={
                    "sequence": seq, "mode": mode, "instruction_preview": instruction[:240],
                }))
                await self._write(writer, response(req, {
                    "accepted": True, "task_id": task_id, "mode": mode, "sequence": seq,
                    "turn": int(row["current_turn"] or 1), "agent_session_id": row["agent_session_id"],
                    "state": "queued",
                }))
                return

            # steer: supersede the active turn before terminating it so the old
            # monitor can never finalize the task with the interrupted result.
            old_turn = int(row["current_turn"] or 1)
            old_seq = int(row["active_instruction_seq"] or 0)
            new_turn = old_turn + 1
            self.store.execute(
                "UPDATE tasks SET state='steering',current_turn=?,active_instruction_seq=?,updated_at=unixepoch() WHERE task_id=?",
                (new_turn, seq, task_id),
            )
            if old_seq:
                self.store.execute(
                    "UPDATE task_instructions SET state='superseded',completed_at=unixepoch() "
                    "WHERE task_id=? AND sequence=? AND state='running'",
                    (task_id, old_seq),
                )
                await self.emit(event("task.turn.superseded", task_id=task_id, source=self.node_name, data={
                    "sequence": old_seq, "turn": old_turn, "superseded_by": seq,
                }))
            await self._terminate_execution_scope(task_id, row, turn=old_turn)
            await self._launch_turn_locked(task_id, seq, new_turn, mode, instruction, resume=True)
            latest = self.store.one("SELECT agent_session_id,session_id FROM tasks WHERE task_id=?", (task_id,))
            await self._write(writer, response(req, {
                "accepted": True, "task_id": task_id, "mode": mode, "sequence": seq,
                "turn": new_turn, "agent_session_id": latest["agent_session_id"],
                "session_id": latest["session_id"], "state": "running",
            }))

    def _task_runtime(self, task_id: str, row=None):
        row = row or self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        task_dir = Path((row["workdir"] if row and row["workdir"] else "") or (self.root / "tasks" / task_id))
        runtime_version = int(row["runtime_version"] or 1) if row and "runtime_version" in row.keys() else 1
        turn = int(row["current_turn"] or 0) if row and "current_turn" in row.keys() else 0
        if runtime_version >= 2 and turn > 0:
            turn_dir = task_dir / "turns" / f"{turn:04d}"
            session = str((row["session_id"] if row and row["session_id"] else "") or self._turn_session(task_id, turn))
            return task_dir, session, turn_dir / "output.log", turn_dir / "exit_code"
        session = str((row["session_id"] if row and row["session_id"] else "") or f"workbot-{task_id[:20]}")
        return task_dir, session, task_dir / "output.log", task_dir / "exit_code"

    @staticmethod
    def _turn_session(task_id: str, turn: int) -> str:
        suffix = task_id.split("task-", 1)[-1][-12:]
        return f"wb-{suffix}-t{int(turn):04d}"

    def _turn_runtime(self, task_id: str, row, turn: int):
        task_dir = Path((row["workdir"] if row and row["workdir"] else "") or (self.root / "tasks" / task_id))
        runtime_version = int(row["runtime_version"] or 1) if "runtime_version" in row.keys() else 1
        if runtime_version < 2:
            session = str(row["session_id"] or f"workbot-{task_id[:20]}")
            return task_dir, session, task_dir, task_dir / "output.log", task_dir / "exit_code", task_dir / "pgid"
        turn_dir = task_dir / "turns" / f"{int(turn):04d}"
        session = self._turn_session(task_id, turn)
        return task_dir, session, turn_dir, turn_dir / "output.log", turn_dir / "exit_code", turn_dir / "pgid"

    async def _tmux_session_exists(self, session: str) -> bool:
        tmux = self.config.get("tmux_command", "tmux")
        try:
            proc = await asyncio.create_subprocess_exec(
                tmux, "has-session", "-t", session,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        except FileNotFoundError:
            return False

    async def _terminate_execution_scope(self, task_id: str, row, *, turn: int | None = None) -> None:
        turn = int(turn or row["current_turn"] or 1)
        _task_dir, session, turn_dir, _log_file, _result_file, pgid_file = self._turn_runtime(task_id, row, turn)
        pgid = None
        try:
            if pgid_file.exists():
                pgid = int(pgid_file.read_text().strip())
        except Exception:
            pgid = None
        if pgid and pgid > 1:
            try:
                os.killpg(pgid, signal.SIGTERM)
                await asyncio.sleep(float(self.config.get("execution_scope_kill_grace_seconds", 1.5)))
            except ProcessLookupError:
                pass
            except PermissionError:
                log.warning("cannot SIGTERM execution pgid=%s task=%s", pgid, task_id)
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                pass
            else:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        tmux = self.config.get("tmux_command", "tmux")
        try:
            proc = await asyncio.create_subprocess_exec(
                tmux, "kill-session", "-t", session,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except FileNotFoundError:
            pass

    def _schedule_monitor(self, task_id: str, session: str, task_dir: Path, *, turn: int | None = None, sequence: int | None = None) -> None:
        key = f"{task_id}:{int(turn or 0)}"
        old = self._task_monitors.get(key)
        if old and not old.done():
            return
        if turn is None:
            row = self.store.one("SELECT current_turn,active_instruction_seq FROM tasks WHERE task_id=?", (task_id,))
            turn = int(row["current_turn"] or 0) if row else 0
            sequence = int(row["active_instruction_seq"] or 0) if row else 0
        if int(turn or 0) == 0 and int(sequence or 0) == 0:
            # Backwards-compatible legacy/recovery call shape; older tests and
            # integrations may monkeypatch _monitor_task(task_id,session,dir).
            coro = self._monitor_task(task_id, session, task_dir)
        else:
            coro = self._monitor_task(task_id, session, task_dir, turn=int(turn or 0), sequence=int(sequence or 0))
        t = asyncio.create_task(coro, name=f"monitor:{task_id}:{turn}")
        self._task_monitors[key] = t
        t.add_done_callback(lambda _t, k=key: self._task_monitors.pop(k, None))

    async def _recover_tasks(self) -> None:
        rows = self.store.all("SELECT * FROM tasks WHERE state IN ('created','running','steering') ORDER BY created_at")
        recovered = []
        lost = []
        for row in rows:
            task_id = str(row["task_id"])
            task_dir, session, log_file, result_file = self._task_runtime(task_id, row)
            turn = int(row["current_turn"] or 0) if "current_turn" in row.keys() else 0
            seq = int(row["active_instruction_seq"] or 0) if "active_instruction_seq" in row.keys() else 0
            if result_file.exists():
                self.store.execute(
                    "UPDATE tasks SET session_id=?,workdir=?,recovery_count=recovery_count+1,"
                    "last_recovered_at=unixepoch(),updated_at=unixepoch() WHERE task_id=?",
                    (session, str(task_dir), task_id),
                )
                self._schedule_monitor(task_id, session, task_dir, turn=turn, sequence=seq)
                recovered.append(task_id)
                continue
            if await self._tmux_session_exists(session):
                self.store.execute(
                    "UPDATE tasks SET state='running',session_id=?,workdir=?,recovery_count=recovery_count+1,"
                    "last_recovered_at=unixepoch(),updated_at=unixepoch() WHERE task_id=?",
                    (session, str(task_dir), task_id),
                )
                self._schedule_monitor(task_id, session, task_dir, turn=turn, sequence=seq)
                recovered.append(task_id)
                continue
            if row["state"] == "created":
                asyncio.create_task(self._run_task(task_id, str(row["task_type"]), str(row["instruction"] or "")), name=f"task:{task_id}")
                recovered.append(task_id)
                continue
            payload = {
                "status": "failed", "error_type": "WorkerRecoveryLostTask",
                "error": "worker restarted but the recorded tmux session/result file no longer exists",
                "session": session, "turn": turn,
            }
            self.store.execute(
                "UPDATE tasks SET recovery_count=recovery_count+1,last_recovered_at=unixepoch(),updated_at=unixepoch() WHERE task_id=?",
                (task_id,),
            )
            await self._commit_terminal("task.failed", "failed", task_id, payload)
            lost.append(task_id)
        if recovered or lost:
            log.info("worker recovery: resumed=%s lost=%s", recovered, lost)

    def _terminal_message(self, event_name: str, task_id: str, payload: dict) -> Message:
        return Message(
            type="event", id=f"evt-{task_id}-{event_name.split('.')[-1]}",
            event=event_name, task_id=task_id, source=self.node_name, data=payload,
        )

    async def _commit_terminal(self, event_name: str, state: str, task_id: str, payload: dict) -> None:
        msg = self._terminal_message(event_name, task_id, payload)
        self.store.finalize_and_enqueue(task_id, state, json.dumps(payload, ensure_ascii=False), msg)
        await self._broadcast(msg)

    async def _finalize_task_from_files(self, task_id: str, session: str, task_dir: Path, *, turn: int = 0, sequence: int = 0) -> None:
        row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not row:
            return
        _root, _session, turn_dir, log_file, result_file, _pgid = self._turn_runtime(task_id, row, turn or int(row["current_turn"] or 1))
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        next_item = None
        terminal = None
        turn_event = None
        async with lock:
            current = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if not current or current["state"] in {"completed", "failed", "cancelled", "cancelling", "steering"}:
                return
            if int(current["current_turn"] or 0) != int(turn or 0):
                return
            if not result_file.exists():
                return
            try:
                exit_code = int(result_file.read_text().strip())
            except Exception:
                exit_code = 1
            text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            summary = text[-4000:]
            if exit_code != 0:
                if sequence:
                    self.store.execute(
                        "UPDATE task_instructions SET state='failed',completed_at=unixepoch() WHERE task_id=? AND sequence=?",
                        (task_id, sequence),
                    )
                payload = {"status": "failed", "exit_code": exit_code, "summary": summary, "session": session, "turn": turn, "sequence": sequence}
                terminal = self._terminal_message("task.failed", task_id, payload)
                self.store.finalize_and_enqueue(task_id, "failed", json.dumps(payload, ensure_ascii=False), terminal)
            else:
                if sequence:
                    self.store.execute(
                        "UPDATE task_instructions SET state='completed',completed_at=unixepoch() WHERE task_id=? AND sequence=?",
                        (task_id, sequence),
                    )
                    turn_event = event("task.turn.completed", task_id=task_id, source=self.node_name, data={
                        "sequence": sequence, "turn": turn, "exit_code": exit_code,
                    })
                queued = self.store.queued_instructions(task_id)
                if queued:
                    next_item = queued[0]
                    new_turn = int(turn) + 1
                    self.store.execute(
                        "UPDATE tasks SET state='running',current_turn=?,active_instruction_seq=?,updated_at=unixepoch() WHERE task_id=?",
                        (new_turn, int(next_item["sequence"]), task_id),
                    )
                else:
                    payload = {"status": "completed", "exit_code": exit_code, "summary": summary, "session": session, "turn": turn, "sequence": sequence}
                    terminal = self._terminal_message("task.completed", task_id, payload)
                    self.store.finalize_and_enqueue(task_id, "completed", json.dumps(payload, ensure_ascii=False), terminal)
        if turn_event:
            self.store.enqueue(turn_event)
            await self._broadcast(turn_event)
        if next_item is not None:
            new_turn = int(turn) + 1
            lock = self._task_locks.setdefault(task_id, asyncio.Lock())
            async with lock:
                latest = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
                if not latest or latest["state"] in {"cancelled", "failed", "completed", "cancelling", "steering"}:
                    return
                if int(latest["current_turn"] or 0) != new_turn or int(latest["active_instruction_seq"] or 0) != int(next_item["sequence"]):
                    return
                await self._launch_turn_locked(
                    task_id, int(next_item["sequence"]), new_turn, str(next_item["mode"]), str(next_item["instruction"]), resume=True
                )
            return
        if terminal:
            await self._broadcast(terminal)

    async def _monitor_task(self, task_id: str, session: str, task_dir: Path, *, turn: int = 0, sequence: int = 0):
        row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not row:
            return
        _root, _session, _turn_dir, _log_file, result_file, _pgid = self._turn_runtime(task_id, row, turn or int(row["current_turn"] or 1))
        missing_session_checks = 0
        while not result_file.exists():
            current = self.store.one("SELECT state,current_turn FROM tasks WHERE task_id=?", (task_id,))
            if not current or current["state"] in {"completed", "failed", "cancelled", "cancelling", "steering"}:
                return
            if int(current["current_turn"] or 0) != int(turn or 0):
                return
            if await self._tmux_session_exists(session):
                missing_session_checks = 0
            else:
                missing_session_checks += 1
                if missing_session_checks >= 3:
                    await asyncio.sleep(0.25)
                    if result_file.exists():
                        break
                    payload = {
                        "status": "failed", "error_type": "TaskProcessLost",
                        "error": "tmux session disappeared before an exit code was recorded",
                        "session": session, "turn": turn, "sequence": sequence,
                    }
                    lock = self._task_locks.setdefault(task_id, asyncio.Lock())
                    async with lock:
                        current = self.store.one("SELECT state,current_turn FROM tasks WHERE task_id=?", (task_id,))
                        if not current or current["state"] in {"completed", "failed", "cancelled", "cancelling", "steering"}:
                            return
                        if int(current["current_turn"] or 0) != int(turn or 0):
                            return
                        if sequence:
                            self.store.execute(
                                "UPDATE task_instructions SET state='failed',completed_at=unixepoch() WHERE task_id=? AND sequence=?",
                                (task_id, sequence),
                            )
                        terminal = self._terminal_message("task.failed", task_id, payload)
                        self.store.finalize_and_enqueue(task_id, "failed", json.dumps(payload, ensure_ascii=False), terminal)
                    await self._broadcast(terminal)
                    return
            await asyncio.sleep(1)
        await self._finalize_task_from_files(task_id, session, task_dir, turn=turn, sequence=sequence)

    def _managed_prompt(self, task_id: str, instruction: str, *, sequence: int, turn: int, mode: str, resumed: bool) -> str:
        continuity = (
            "This is a later turn of the SAME WorkBot task and MUST resume the same CodeAgent session. "
            "Use prior session context, but the new instruction below is authoritative for what to do next.\n"
            if resumed else
            "This is the first turn of this WorkBot task.\n"
        )
        steering = ""
        if mode == "steer":
            steering = (
                "The previous managed execution turn was deliberately interrupted by WorkBot. Its managed process group was terminated. "
                "Do NOT continue the superseded execution (for example an old release fastcheck); follow the new correction below.\n"
            )
        elif mode == "append":
            steering = "The previous turn completed; this is an appended follow-up instruction within the same task.\n"
        return (
            f"You are executing WorkBot task {task_id} on this Linux node. This task ID is authoritative.\n"
            f"Task instruction sequence={sequence}, turn={turn}, mode={mode}.\n" + continuity + steering +
            "Follow this node's AGENTS.md and relevant skills. Do not claim that you are another task ID.\n"
            "Do not inspect or reuse sibling ~/.workbot/tasks/task-* outputs unless the user explicitly asks for that historical task.\n"
            "The local agent-worker automatically reports final task completion or failure after the FINAL turn exits.\n"
            "Do NOT call `agentctl notify` for final completion/failure. Intermediate notifications should be sparse and meaningful.\n"
            "This managed task prepends ~/.workbot/enforced-bin to PATH. Never bypass policy wrappers.\n"
            "For policy-sensitive side effects, use the request-approval/gated-actions skills as instructed.\n\n"
            "--- Authoritative instruction for this turn ---\n" + instruction
        )

    async def _launch_turn_locked(self, task_id: str, sequence: int, turn: int, mode: str, instruction: str, *, resume: bool) -> None:
        row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not row:
            raise RuntimeError(f"unknown task {task_id}")
        task_dir = self.root / "tasks" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        root_agents = self.root / "AGENTS.md"
        if root_agents.exists():
            try:
                (task_dir / "AGENTS.md").write_text(root_agents.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            except OSError:
                log.exception("failed to stage task AGENTS.md for %s", task_id)
        _root, session, turn_dir, log_file, result_file, pgid_file = self._turn_runtime(task_id, row, turn)
        turn_dir.mkdir(parents=True, exist_ok=True)
        prompt = turn_dir / "prompt.txt"
        prompt.write_text(self._managed_prompt(task_id, instruction, sequence=sequence, turn=turn, mode=mode, resumed=resume), encoding="utf-8")
        codeagent = self.config.get("codeagent_command", "codeagent")
        agent_session_id = str(row["agent_session_id"] or "") or str(uuid.uuid4())
        if not row["agent_session_id"]:
            self.store.execute("UPDATE tasks SET agent_session_id=? WHERE task_id=?", (agent_session_id, task_id))
        if resume:
            template = self.config.get(
                "codeagent_resume_template",
                '{codeagent} --sessions {session_id} -p "$(cat {prompt})" --skip-safe-check',
            )
        else:
            template = self.config.get(
                "codeagent_new_template",
                '{codeagent} --session-id {session_id} -p "$(cat {prompt})" --skip-safe-check',
            )
        cmd = template.format(
            codeagent=shlex.quote(codeagent), prompt=shlex.quote(str(prompt)), task_id=task_id,
            session_id=shlex.quote(agent_session_id),
        )
        if resume and "{session_id}" not in template:
            cmd += " --sessions " + shlex.quote(agent_session_id)
        if not resume and "{session_id}" not in template:
            cmd += " --session-id " + shlex.quote(agent_session_id)

        cwd_mode = str(self.config.get("agent_cwd_mode", "task")).lower()
        if cwd_mode == "configured" and self.config.get("agent_cwd"):
            agent_cwd = Path(os.path.expanduser(str(self.config.get("agent_cwd"))))
        else:
            agent_cwd = task_dir
        conversation_id = str(row["conversation_id"] or "")
        env_lines = (
            f"export WORKBOT_TASK_ID={shlex.quote(task_id)}\n"
            f"export WORKBOT_TASK_TURN={int(turn)}\n"
            f"export WORKBOT_INSTRUCTION_SEQ={int(sequence)}\n"
            f"export WORKBOT_NODE={shlex.quote(self.node_name)}\n"
            f"export WORKBOT_CONVERSATION_ID={shlex.quote(conversation_id)}\n"
            f"export WORKBOT_SOCKET={shlex.quote(str(self.socket_path))}\n"
            f"export WORKBOT_MANAGED_TASK=1\n"
            f"export PATH={shlex.quote(str(self.root / 'enforced-bin'))}:{shlex.quote(str(self.root / 'bin'))}:\"$PATH\"\n"
            f"cd {shlex.quote(str(agent_cwd))}\n"
        )
        wrapper = turn_dir / "run.sh"
        inner = shlex.quote(cmd)
        wrapper.write_text(
            "#!/usr/bin/env bash\nset +e\n" + env_lines +
            "if ! command -v setsid >/dev/null 2>&1; then echo 'WorkBot requires setsid for managed task process-scope control' >&2; exit 127; fi\n"
            f"setsid bash -lc {inner} > {shlex.quote(str(log_file))} 2>&1 &\n"
            "scope_pid=$!\n"
            f"echo $scope_pid > {shlex.quote(str(pgid_file))}\n"
            "wait $scope_pid\n"
            "rc=$?\n"
            f"echo $rc > {shlex.quote(str(result_file))}\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        tmux = self.config.get("tmux_command", "tmux")
        proc = await asyncio.create_subprocess_exec(tmux, "new-session", "-d", "-s", session, str(wrapper))
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(f"tmux failed to create turn session rc={rc}")
        self.store.execute(
            "UPDATE tasks SET state='running',session_id=?,workdir=?,runtime_version=2,current_turn=?,active_instruction_seq=?,updated_at=unixepoch() WHERE task_id=?",
            (session, str(task_dir), turn, sequence, task_id),
        )
        self.store.execute(
            "UPDATE task_instructions SET state='running',started_at=COALESCE(started_at,unixepoch()) WHERE task_id=? AND sequence=?",
            (task_id, sequence),
        )
        started = event("task.turn.started", task_id=task_id, source=self.node_name, data={
            "sequence": sequence, "turn": turn, "mode": mode, "agent_session_id": agent_session_id,
        })
        self.store.enqueue(started)
        await self._broadcast(started)
        self._schedule_monitor(task_id, session, task_dir, turn=turn, sequence=sequence)

    async def _run_task(self, task_id: str, task_type: str, instruction: str):
        initial = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if initial and initial["state"] == "cancelled":
            return
        await self.emit(event("task.started", task_id=task_id, source=self.node_name, data={"status": "running"}))
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        try:
            async with lock:
                row = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
                if not row or row["state"] == "cancelled":
                    return
                if task_type == "shell":
                    # Shell tasks remain one-shot for compatibility; steering is
                    # intentionally a CodeAgent-task feature.
                    self.store.execute("UPDATE tasks SET state='running',updated_at=unixepoch() WHERE task_id=?", (task_id,))
                    raise RuntimeError("shell managed tasks are not steerable in V1.3; use codeagent tasks")
                await self._launch_turn_locked(task_id, 1, 1, "initial", instruction, resume=False)
        except Exception as exc:
            payload = {"status": "failed", "error": str(exc)}
            async with lock:
                current = self.store.one("SELECT state FROM tasks WHERE task_id=?", (task_id,))
                if current and current["state"] in {"cancelled", "cancelling"}:
                    return
                terminal = self._terminal_message("task.failed", task_id, payload)
                self.store.finalize_and_enqueue(task_id, "failed", json.dumps(payload, ensure_ascii=False), terminal)
            await self._broadcast(terminal)

    async def emit(self, msg: Message):
        self.store.enqueue(msg)
        await self._broadcast(msg)

    async def _broadcast(self, msg: Message):
        dead = []
        for writer in list(self.upstreams):
            try:
                await self._write(writer, msg)
            except Exception:
                dead.append(writer)
        for w in dead:
            self.upstreams.discard(w)

    async def _send_pending(self, writer: asyncio.StreamWriter):
        for row in self.store.pending():
            await self._write(writer, Message.loads(row["payload_json"]))

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, msg: Message):
        writer.write((msg.dumps() + "\n").encode("utf-8"))
        await writer.drain()
