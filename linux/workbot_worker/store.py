from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  task_type TEXT NOT NULL,
  title TEXT,
  instruction TEXT,
  conversation_id TEXT,
  state TEXT NOT NULL,
  session_id TEXT,
  agent_session_id TEXT,
  workdir TEXT,
  result_json TEXT,
  runtime_version INTEGER NOT NULL DEFAULT 1,
  current_turn INTEGER NOT NULL DEFAULT 0,
  active_instruction_seq INTEGER,
  recovery_count INTEGER NOT NULL DEFAULT 0,
  last_recovered_at INTEGER,
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS task_instructions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  mode TEXT NOT NULL,
  instruction TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  started_at INTEGER,
  completed_at INTEGER,
  UNIQUE(task_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_worker_task_instructions ON task_instructions(task_id, sequence);

CREATE TABLE IF NOT EXISTS outbox (
  event_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  delivered_at INTEGER
);
"""


class WorkerStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        conn = self._open()
        try:
            conn.executescript(SCHEMA)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            if "conversation_id" not in existing:
                conn.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT")
            if "agent_session_id" not in existing:
                conn.execute("ALTER TABLE tasks ADD COLUMN agent_session_id TEXT")
            if "recovery_count" not in existing:
                conn.execute("ALTER TABLE tasks ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0")
            if "last_recovered_at" not in existing:
                conn.execute("ALTER TABLE tasks ADD COLUMN last_recovered_at INTEGER")
            if "runtime_version" not in existing:
                conn.execute("ALTER TABLE tasks ADD COLUMN runtime_version INTEGER NOT NULL DEFAULT 1")
            if "current_turn" not in existing:
                conn.execute("ALTER TABLE tasks ADD COLUMN current_turn INTEGER NOT NULL DEFAULT 0")
            if "active_instruction_seq" not in existing:
                conn.execute("ALTER TABLE tasks ADD COLUMN active_instruction_seq INTEGER")
            conn.commit()
        finally:
            conn.close()

    def _open(self):
        c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def execute(self, sql, params=()):
        c = self._open()
        try:
            cur = c.execute(sql, params)
            c.commit()
            return cur.rowcount
        finally:
            c.close()

    def one(self, sql, params=()):
        c = self._open()
        try:
            return c.execute(sql, params).fetchone()
        finally:
            c.close()

    def all(self, sql, params=()):
        c = self._open()
        try:
            return list(c.execute(sql, params).fetchall())
        finally:
            c.close()

    def next_instruction_sequence(self, task_id: str) -> int:
        row = self.one("SELECT COALESCE(MAX(sequence),0)+1 AS seq FROM task_instructions WHERE task_id=?", (task_id,))
        return int(row["seq"] if row else 1)

    def instruction(self, task_id: str, sequence: int):
        return self.one("SELECT * FROM task_instructions WHERE task_id=? AND sequence=?", (task_id, int(sequence)))

    def queued_instructions(self, task_id: str):
        return self.all(
            "SELECT * FROM task_instructions WHERE task_id=? AND state='queued' ORDER BY CASE WHEN mode='steer' THEN 0 ELSE 1 END, sequence",
            (task_id,),
        )

    def enqueue(self, msg) -> None:
        self.execute(
            "INSERT OR IGNORE INTO outbox(event_id,payload_json,state) VALUES (?,?,'pending')",
            (msg.id, msg.dumps()),
        )

    def finalize_and_enqueue(self, task_id: str, state: str, result_json: str, msg) -> None:
        """Atomically persist terminal task state and its durable outbox event."""
        c = self._open()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE tasks SET state=?,result_json=?,updated_at=unixepoch() WHERE task_id=?",
                (state, result_json, task_id),
            )
            c.execute(
                "INSERT OR IGNORE INTO outbox(event_id,payload_json,state) VALUES (?,?,'pending')",
                (msg.id, msg.dumps()),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def pending(self):
        return self.all("SELECT event_id,payload_json FROM outbox WHERE state='pending' ORDER BY created_at")

    def ack(self, event_id: str):
        self.execute("UPDATE outbox SET state='delivered', delivered_at=unixepoch() WHERE event_id=?", (event_id,))
