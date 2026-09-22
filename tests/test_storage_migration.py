import sqlite3
import tempfile
import unittest
from pathlib import Path

from workbot.storage.sqlite import Store


class StorageMigrationTests(unittest.TestCase):
    def test_v02_tasks_table_gets_workflow_columns(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript('''
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                origin_conversation_id TEXT,
                origin_message_id TEXT,
                parent_task_id TEXT,
                node TEXT NOT NULL,
                task_type TEXT NOT NULL,
                state TEXT NOT NULL,
                title TEXT,
                instruction TEXT,
                result_json TEXT,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                updated_at INTEGER NOT NULL DEFAULT (unixepoch())
            );
            ''')
            conn.commit(); conn.close()
            Store(path)
            conn = sqlite3.connect(path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            conn.close()
            self.assertIn("workflow_id", cols)
            self.assertIn("workflow_step_id", cols)
            self.assertIn("notify_mode", cols)


if __name__ == "__main__":
    unittest.main()


def test_v03_conversation_and_memory_tables_get_v06_columns(tmp_path):
    path = tmp_path / "old-v03.db"
    conn = sqlite3.connect(path)
    conn.executescript('''
    CREATE TABLE conversations (
      conversation_id TEXT PRIMARY KEY, platform TEXT NOT NULL, kind TEXT NOT NULL,
      external_id TEXT NOT NULL, display_name TEXT, agent_session_id TEXT, summary TEXT NOT NULL DEFAULT '',
      pending_action_json TEXT, last_message_id TEXT, updated_at INTEGER NOT NULL DEFAULT (unixepoch())
    );
    CREATE TABLE memory_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, title TEXT NOT NULL,
      content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '', source TEXT,
      confidence REAL NOT NULL DEFAULT 1.0, created_at INTEGER NOT NULL DEFAULT (unixepoch()),
      updated_at INTEGER NOT NULL DEFAULT (unixepoch())
    );
    CREATE TABLE tasks (
      task_id TEXT PRIMARY KEY, origin_conversation_id TEXT, origin_message_id TEXT, parent_task_id TEXT,
      node TEXT NOT NULL, task_type TEXT NOT NULL, state TEXT NOT NULL, title TEXT, instruction TEXT,
      result_json TEXT, created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())
    );
    ''')
    conn.execute("INSERT INTO memory_items(scope,title,content) VALUES ('global','legacy','old memory')")
    conn.commit(); conn.close()
    store = Store(path)
    conn = sqlite3.connect(path)
    conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    mem_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
    conn.close()
    assert {"summary_message_count","session_turns","session_updated_at"}.issubset(conv_cols)
    assert {"memory_kind","fingerprint","last_used_at"}.issubset(mem_cols)
    assert store.query_one("SELECT title FROM memory_items WHERE title='legacy'")["title"] == "legacy"
