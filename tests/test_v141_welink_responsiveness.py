import importlib.util
import os
from pathlib import Path

from workbot.im.welink import WeLinkAdapter

ROOT = Path(__file__).resolve().parents[1]


class DiscoveryProbe(WeLinkAdapter):
    def __init__(self):
        super().__init__(
            "welink-cli",
            [{"group_id": "important", "group_name": "important"}, {"group_id": "quiet", "group_name": "quiet"}],
            bootstrap_from_latest=False,
            discovery={
                "mode": "all",
                "refresh_seconds": 3,
                "active_interval_seconds": 5,
                "priority_groups": ["important"],
                "recent_focus_count": 3,
                "history_min_interval_seconds": 60,
            },
        )
        self.recent_calls = 0

    def _run_json(self, *args):
        if args[:2] == ("im", "query-recent-conversation"):
            self.recent_calls += 1
            return {"conversation_info": [
                {"group_id": "important", "group_name": "important"},
                {"group_id": "quiet", "group_name": "quiet"},
            ]}
        return {"respData": {"chatInfo": []}}


def test_recent_conversation_discovery_is_periodic_and_fast():
    a = DiscoveryProbe()
    a._discover_conversations()
    assert a.recent_calls == 1
    assert a._discovery_interval == 3
    a._discover_conversations()
    assert a.recent_calls == 1
    a._next_discovery_at = 0
    a._discover_conversations()
    assert a.recent_calls == 2


def test_important_recent_conversation_beats_older_quiet_room():
    a = DiscoveryProbe()
    now = 1000.0
    important = a._conversations["group:important"]
    quiet = a._conversations["group:quiet"]
    important.recent_rank = 0
    important.recent_seen_at = now
    important.next_poll_at = now - 1
    important.last_polled_at = now - 5
    quiet.recent_rank = 999
    quiet.next_poll_at = now - 100
    quiet.last_polled_at = now - 100
    assert a._conversation_priority(important, now) < a._conversation_priority(quiet, now)


def test_reply_boost_is_top_priority():
    a = DiscoveryProbe()
    now = 1000.0
    quiet = a._conversations["group:quiet"]
    quiet.boost_until = now + 60
    assert a._conversation_priority(quiet, now)[0] == 0


def test_gateway_windows_cmd_launcher_shape(monkeypatch, tmp_path: Path):
    path = ROOT / "scripts" / "workbot-welink-gateway.py"
    spec = importlib.util.spec_from_file_location("workbot_welink_gateway", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    fake = tmp_path / "fake-welink.cmd"
    fake.write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setenv("ComSpec", r"C:\\Windows\\System32\\cmd.exe")
    cmd = mod.welink_launcher(str(fake), ["search", "person", "--text", "Alice"])
    assert cmd[:4] == [r"C:\\Windows\\System32\\cmd.exe", "/d", "/s", "/c"]
    assert "fake-welink.cmd" in cmd[4]
    assert "search" in cmd[4]
