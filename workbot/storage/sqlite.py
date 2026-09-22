from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(slots=True)
class ExecuteResult:
    lastrowid: int | None
    rowcount: int


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    display_name TEXT,
    agent_session_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    summary_message_count INTEGER NOT NULL DEFAULT 0,
    memory_message_count INTEGER NOT NULL DEFAULT 0,
    session_turns INTEGER NOT NULL DEFAULT 0,
    session_updated_at INTEGER,
    pending_action_json TEXT,
    last_message_id TEXT,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    sender_id TEXT,
    direction TEXT NOT NULL,
    content TEXT NOT NULL,
    sent_at INTEGER,
    transport TEXT,
    dedup_key TEXT,
    content_key TEXT,
    context_json TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(conversation_id, external_message_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    origin_conversation_id TEXT,
    origin_message_id TEXT,
    requested_by TEXT,
    authorized_by TEXT,
    authorization_message_id TEXT,
    parent_task_id TEXT,
    node TEXT NOT NULL,
    task_type TEXT NOT NULL,
    state TEXT NOT NULL,
    title TEXT,
    instruction TEXT,
    result_json TEXT,
    agent_session_id TEXT,
    current_turn INTEGER NOT NULL DEFAULT 0,
    active_instruction_seq INTEGER,
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

CREATE INDEX IF NOT EXISTS idx_task_instructions_task ON task_instructions(task_id, sequence);

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    origin_conversation_id TEXT,
    origin_message_id TEXT,
    requested_by TEXT,
    authorized_by TEXT,
    authorization_message_id TEXT,
    state TEXT NOT NULL,
    summary TEXT,
    original_instruction TEXT NOT NULL,
    result_json TEXT,
    recovery_reason TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    workflow_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    executor TEXT NOT NULL,
    node TEXT,
    instruction TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    notify_on_complete INTEGER NOT NULL DEFAULT 0,
    milestone TEXT,
    result_text TEXT,
    task_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY(workflow_id, step_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    node TEXT,
    task_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS processed_messages (
    platform TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    processed_at INTEGER NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY(platform, external_message_id)
);

CREATE TABLE IF NOT EXISTS memory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'fact',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    source TEXT,
    fingerprint TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence TEXT,
    auto_generated INTEGER NOT NULL DEFAULT 0,
    promotion_state TEXT NOT NULL DEFAULT 'none',
    promotion_reviewed_at INTEGER,
    promoted_global_id INTEGER,
    last_used_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_tasks_conversation ON tasks(origin_conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workflows_conversation ON workflows(origin_conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    node TEXT,
    task_id TEXT,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    requested_by TEXT,
    decided_by TEXT,
    reason TEXT,
    expires_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    decided_at INTEGER,
    resume_state TEXT NOT NULL DEFAULT 'none',
    resumed_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_approvals_conversation ON approval_requests(conversation_id, state, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approval_requests(task_id, state, created_at);

CREATE TABLE IF NOT EXISTS outbound_messages (
    send_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    delivered_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_outbound_pending ON outbound_messages(state, next_attempt_at, created_at_ms);

CREATE TABLE IF NOT EXISTS workspace_projects (
    root_path TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    fingerprint TEXT,
    analyzed_fingerprint TEXT,
    summary TEXT NOT NULL DEFAULT '',
    structure TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    file_count INTEGER NOT NULL DEFAULT 0,
    last_scanned_at INTEGER,
    last_analyzed_at INTEGER,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS workspace_files (
    path TEXT PRIMARY KEY,
    project_root TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    file_kind TEXT NOT NULL DEFAULT 'text',
    indexed_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_workspace_files_project ON workspace_files(project_root, relative_path);

CREATE TABLE IF NOT EXISTS workspace_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    project_root TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_workspace_chunks_path ON workspace_chunks(path, chunk_index);
CREATE INDEX IF NOT EXISTS idx_workspace_chunks_project ON workspace_chunks(project_root, path);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fts5_available = False
        self.workspace_fts_available = False
        self._init()

    def _open(self) -> sqlite3.Connection:
        # Use short-lived connections.  This avoids leaving SQLite file handles
        # open across Windows TemporaryDirectory cleanup and is also safer for
        # WorkBot's asyncio/thread-pool mix than a thread-local long-lived
        # connection. WAL keeps concurrent readers/writers inexpensive.
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(SCHEMA)
            # V0.2/V0.3 migrations.
            self._ensure_column(conn, "tasks", "workflow_id", "TEXT")
            self._ensure_column(conn, "tasks", "workflow_step_id", "TEXT")
            self._ensure_column(conn, "tasks", "notify_mode", "TEXT NOT NULL DEFAULT 'direct'")
            self._ensure_column(conn, "tasks", "agent_session_id", "TEXT")
            self._ensure_column(conn, "tasks", "current_turn", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "tasks", "active_instruction_seq", "INTEGER")
            # V1.11.3 delegated execution provenance. A task may be requested by
            # one chat participant and later authorized/created by an operator.
            self._ensure_column(conn, "tasks", "requested_by", "TEXT")
            self._ensure_column(conn, "tasks", "authorized_by", "TEXT")
            self._ensure_column(conn, "tasks", "authorization_message_id", "TEXT")
            self._ensure_column(conn, "workflows", "requested_by", "TEXT")
            self._ensure_column(conn, "workflows", "authorized_by", "TEXT")
            self._ensure_column(conn, "workflows", "authorization_message_id", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id, workflow_step_id)")
            # V0.4 lifecycle/recovery migrations.
            self._ensure_column(conn, "workflows", "recovery_reason", "TEXT")
            self._ensure_column(conn, "workflow_steps", "attempts", "INTEGER NOT NULL DEFAULT 0")
            # V0.5 conversation/session migrations.
            self._ensure_column(conn, "conversations", "summary_message_count", "INTEGER NOT NULL DEFAULT 0")
            # V1.2.1: V1.2 maintenance used memory_message_count but the first
            # V1.2 schema/migration accidentally omitted the column.
            self._ensure_column(conn, "conversations", "memory_message_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "conversations", "session_turns", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "conversations", "session_updated_at", "INTEGER")
            # V1.5.2 cross-transport message identity. WeLinkBot push and
            # welink-cli history may expose different message IDs for the same
            # logical message, so durable dedup cannot rely on external ID only.
            self._ensure_column(conn, "messages", "transport", "TEXT")
            self._ensure_column(conn, "messages", "dedup_key", "TEXT")
            self._ensure_column(conn, "messages", "content_key", "TEXT")
            # V1.12 rich WeLink message context: normalized quote metadata and
            # best-effort local image/OCR results. Old databases migrate in-place.
            self._ensure_column(conn, "messages", "context_json", "TEXT")
            self._ensure_column(conn, "approval_requests", "resume_state", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(conn, "approval_requests", "resumed_at", "INTEGER")
            # V1.6 workspace knowledge: distinguish a successfully analyzed
            # project fingerprint from a merely scanned one so transient
            # maintenance-agent failures are retried without waiting a full
            # filesystem scan interval.
            self._ensure_column(conn, "workspace_projects", "analyzed_fingerprint", "TEXT")
            # V1.6.1: relative_path is a logical/search path, not an OS access
            # path. Normalize legacy Windows backslashes so retrieval, tests,
            # project summaries, and cross-platform tooling see one representation.
            conn.execute("UPDATE workspace_files SET relative_path=REPLACE(relative_path, char(92), '/') WHERE instr(relative_path, char(92)) > 0")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedup_key "
                "ON messages(conversation_id, dedup_key) WHERE dedup_key IS NOT NULL"
            )
            # V0.6 memory migrations.
            self._ensure_column(conn, "memory_items", "memory_kind", "TEXT NOT NULL DEFAULT 'fact'")
            self._ensure_column(conn, "memory_items", "fingerprint", "TEXT")
            self._ensure_column(conn, "memory_items", "last_used_at", "INTEGER")
            self._ensure_column(conn, "memory_items", "evidence", "TEXT")
            self._ensure_column(conn, "memory_items", "auto_generated", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "memory_items", "promotion_state", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(conn, "memory_items", "promotion_reviewed_at", "INTEGER")
            self._ensure_column(conn, "memory_items", "promoted_global_id", "INTEGER")
            conn.execute("UPDATE memory_items SET promotion_state='candidate' WHERE auto_generated=1 AND scope LIKE 'conversation:%' AND promotion_state='none'")
            # Memories created by V0.6 automatic capture are recognizable by
            # their source prefix. Mark them for review/purge without changing
            # manually-created /remember entries.
            conn.execute("UPDATE memory_items SET auto_generated=1 WHERE source LIKE 'summary:%' OR source LIKE 'workflow:%'")
            self._backfill_memory_fingerprints(conn)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_fingerprint "
                "ON memory_items(fingerprint) WHERE fingerprint IS NOT NULL"
            )
            self._init_fts(conn)
            self._init_workspace_fts(conn)
            conn.commit()
        finally:
            conn.close()


    def _backfill_memory_fingerprints(self, conn: sqlite3.Connection) -> None:
        existing = {r[0] for r in conn.execute("SELECT fingerprint FROM memory_items WHERE fingerprint IS NOT NULL")}
        rows = list(conn.execute("SELECT id,scope,title,content FROM memory_items WHERE fingerprint IS NULL ORDER BY id"))
        for row in rows:
            fp = self.memory_fingerprint(row[1], row[2], row[3])
            if fp in existing:
                # Old releases allowed duplicates. Keep the older canonical row
                # and remove the duplicate during migration.
                conn.execute("DELETE FROM memory_items WHERE id=?", (row[0],))
                continue
            conn.execute("UPDATE memory_items SET fingerprint=? WHERE id=?", (fp, row[0]))
            existing.add(fp)

    def _init_fts(self, conn: sqlite3.Connection) -> None:
        """Create a contentless-ish FTS mirror when SQLite was built with FTS5.

        FTS is an optimization only.  Every caller has a LIKE fallback so a
        corporate Python build without FTS5 remains supported.
        """
        try:
            conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    title, content, tags, scope, memory_kind,
                    content='memory_items', content_rowid='id',
                    tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_items BEGIN
                  INSERT INTO memory_fts(rowid,title,content,tags,scope,memory_kind)
                  VALUES (new.id,new.title,new.content,new.tags,new.scope,new.memory_kind);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_items BEGIN
                  INSERT INTO memory_fts(memory_fts,rowid,title,content,tags,scope,memory_kind)
                  VALUES ('delete',old.id,old.title,old.content,old.tags,old.scope,old.memory_kind);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_items BEGIN
                  INSERT INTO memory_fts(memory_fts,rowid,title,content,tags,scope,memory_kind)
                  VALUES ('delete',old.id,old.title,old.content,old.tags,old.scope,old.memory_kind);
                  INSERT INTO memory_fts(rowid,title,content,tags,scope,memory_kind)
                  VALUES (new.id,new.title,new.content,new.tags,new.scope,new.memory_kind);
                END;
                """
            )
            # Rebuild is idempotent and migrates rows created before FTS existed.
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            self.fts5_available = True
        except sqlite3.OperationalError:
            self.fts5_available = False


    def _init_workspace_fts(self, conn: sqlite3.Connection) -> None:
        """Create an FTS mirror for bounded workspace text chunks.

        Workspace search is an optimization over the durable project/file index;
        LIKE fallbacks keep the feature usable when a corporate SQLite build has
        no FTS5 support.
        """
        try:
            conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS workspace_fts USING fts5(
                    path, project_root, content,
                    content='workspace_chunks', content_rowid='id',
                    tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS workspace_ai AFTER INSERT ON workspace_chunks BEGIN
                  INSERT INTO workspace_fts(rowid,path,project_root,content)
                  VALUES (new.id,new.path,new.project_root,new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS workspace_ad AFTER DELETE ON workspace_chunks BEGIN
                  INSERT INTO workspace_fts(workspace_fts,rowid,path,project_root,content)
                  VALUES ('delete',old.id,old.path,old.project_root,old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS workspace_au AFTER UPDATE ON workspace_chunks BEGIN
                  INSERT INTO workspace_fts(workspace_fts,rowid,path,project_root,content)
                  VALUES ('delete',old.id,old.path,old.project_root,old.content);
                  INSERT INTO workspace_fts(rowid,path,project_root,content)
                  VALUES (new.id,new.path,new.project_root,new.content);
                END;
                """
            )
            conn.execute("INSERT INTO workspace_fts(workspace_fts) VALUES('rebuild')")
            self.workspace_fts_available = True
        except sqlite3.OperationalError:
            self.workspace_fts_available = False

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> ExecuteResult:
        conn = self._open()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return ExecuteResult(lastrowid=cur.lastrowid, rowcount=cur.rowcount)
        finally:
            conn.close()

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        conn = self._open()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        conn = self._open()
        try:
            return list(conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        conn = self._open()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        # Backwards-compatible no-op: V0.6.2 uses short-lived connections.
        return None

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def loads(value: str | None, default: Any = None) -> Any:
        if not value:
            return default
        return json.loads(value)

    @staticmethod
    def memory_fingerprint(scope: str, title: str, content: str) -> str:
        normalized = "\n".join((scope.strip().lower(), title.strip().lower(), " ".join(content.split()).lower()))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
