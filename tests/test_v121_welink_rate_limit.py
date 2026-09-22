import sqlite3
import time
from pathlib import Path

from workbot.im.welink import WeLinkAdapter
from workbot.storage.sqlite import Store


class PacingProbe(WeLinkAdapter):
    def __init__(self):
        super().__init__(
            "welink-cli",
            [
                {"group_id": "important", "group_name": "important"},
                {"group_id": "g2", "group_name": "g2"},
                {"group_id": "g3", "group_name": "g3"},
            ],
            bootstrap_from_latest=False,
            discovery={
                "history_min_interval_seconds": 60,
                "max_history_queries_per_poll": 1,
                "priority_groups": ["important"],
            },
        )
        self.calls = []

    def _run_json(self, *args):
        self.calls.append(args)
        return {"respData": {"chatInfo": []}}


def test_history_fanout_is_paced_one_query_per_slot():
    a = PacingProbe()
    a._poll_sync()
    assert len(a.calls) == 1
    assert "important" in a.calls[0]
    # A second WorkBot poll during the global history interval performs no CLI call.
    a._poll_sync()
    assert len(a.calls) == 1


class RateLimitProbe(WeLinkAdapter):
    def __init__(self):
        super().__init__(
            "welink-cli",
            [{"group_id": "g1"}, {"group_id": "g2"}],
            bootstrap_from_latest=False,
            discovery={"history_min_interval_seconds": 0.5, "rate_limit_cooldown_seconds": 30},
        )
        self.calls = 0

    def _run_json(self, *args):
        self.calls += 1
        raise RuntimeError("welink-cli failed (1): Error: Query message history failed (status 429)")


def test_429_stops_polling_and_enters_global_cooldown():
    a = RateLimitProbe()
    assert a._poll_sync() == []
    assert a.calls == 1
    assert a._rate_limit_hits == 1
    assert a._rate_limit_until > time.monotonic()
    # During cooldown, do not hammer another conversation.
    assert a._poll_sync() == []
    assert a.calls == 1


def test_v12_database_migrates_missing_memory_message_count(tmp_path: Path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE conversations ("
        "conversation_id TEXT PRIMARY KEY, platform TEXT NOT NULL, kind TEXT NOT NULL, "
        "external_id TEXT NOT NULL, display_name TEXT, agent_session_id TEXT, "
        "summary TEXT NOT NULL DEFAULT '', summary_message_count INTEGER NOT NULL DEFAULT 0, "
        "session_turns INTEGER NOT NULL DEFAULT 0, session_updated_at INTEGER, "
        "pending_action_json TEXT, last_message_id TEXT, updated_at INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    Store(db)
    conn = sqlite3.connect(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    conn.close()
    assert "memory_message_count" in cols
