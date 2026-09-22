from __future__ import annotations

from workbot.im.welink import WeLinkAdapter


def _adapter() -> WeLinkAdapter:
    a = WeLinkAdapter(
        "welink-cli",
        [],
        discovery={
            "mode": "all",
            "refresh_seconds": 3,
            "history_budget_per_minute": 18,
            "history_limit_per_minute": 20,
            "max_history_queries_per_poll": 1,
            "recent_count": 20,
        },
    )
    # Test sweep semantics without sleeping for rate pacing.
    a._history_min_interval_seconds = 0.0
    return a


def _reset_history_slot(a: WeLinkAdapter) -> None:
    a._next_history_query_at = 0.0
    a._rate_limit_until = 0.0
    a._history_query_times.clear()


def test_reconnect_backfill_discovers_once_and_queries_each_recent_conversation_once(monkeypatch):
    a = _adapter()
    calls: list[tuple[str, ...]] = []

    def fake_run_json(*args: str):
        calls.append(tuple(args))
        if args[:2] == ("im", "query-recent-conversation"):
            return {
                "conversation_info": [
                    {"groupId": "g1", "groupName": "G1"},
                    {"groupId": "g2", "groupName": "G2"},
                ]
            }
        if args[:2] == ("im", "query-history-message"):
            return {"respData": {"chatInfo": []}}
        raise AssertionError(args)

    monkeypatch.setattr(a, "_run_json", fake_run_json)
    a.request_backfill()
    assert a.backfill_active

    a._poll_sync()
    assert a.backfill_active
    assert a.backfill_pending_count == 1

    _reset_history_slot(a)
    a._poll_sync()
    assert not a.backfill_active
    assert a.backfill_pending_count == 0

    recent_calls = [c for c in calls if c[:2] == ("im", "query-recent-conversation")]
    history_calls = [c for c in calls if c[:2] == ("im", "query-history-message")]
    assert len(recent_calls) == 1
    assert len(history_calls) == 2
    queried = {c[c.index("--group-id") + 1] for c in history_calls}
    assert queried == {"g1", "g2"}

    # Immediately polling again after completion should not re-query either
    # conversation: their normal warm interval is now in the future and recent
    # discovery is still inside its refresh interval.
    _reset_history_slot(a)
    a._poll_sync()
    assert len([c for c in calls if c[:2] == ("im", "query-history-message")]) == 2
    assert len([c for c in calls if c[:2] == ("im", "query-recent-conversation")]) == 1


def test_cancel_backfill_returns_adapter_to_realtime_quiet_state():
    a = _adapter()
    a.request_backfill()
    assert a.backfill_active
    a.cancel_backfill()
    assert not a.backfill_active
    assert a.backfill_pending_count == 0
