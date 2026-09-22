import json
from pathlib import Path

import pytest

from workbot.im.welink import WeLinkAdapter, WeLinkSendAmbiguousError
from workbot.main import WorkBot


class FlakyWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__("welink-cli", [{"group_id": "g1", "group_name": "g1"}], bootstrap_from_latest=False)
        self.fail = True
        self.calls = []

    async def send_text(self, conversation_id: str, text: str, *, created_at_ms=None, verify_before_send=False):
        self.calls.append((conversation_id, text, verify_before_send))
        if self.fail:
            raise WeLinkSendAmbiguousError("verification timeout")


def make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir()
    cfg = {
        "database": "state/workbot.db",
        "access_control": {"default": "allow"},
        "im": {"type": "welink", "groups": [{"group_id": "g1", "group_name": "g1"}]},
        "agent": {"command": "codeagent"},
        "nodes": {},
        "outbound": {"retry_seconds": 1, "retry_max_seconds": 2},
    }
    p = tmp_path / "config" / "local.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return WorkBot(p)


@pytest.mark.asyncio
async def test_transient_send_is_durable_and_later_delivered(tmp_path: Path):
    bot = make_bot(tmp_path)
    flaky = FlakyWeLink()
    bot.im = flaky
    await bot._send_text("welink:group:g1", "hello")
    row = bot.store.query_one("SELECT * FROM outbound_messages ORDER BY created_at_ms DESC LIMIT 1")
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    flaky.fail = False
    await bot._attempt_outbound(row["send_id"], row["conversation_id"], row["text"], row["created_at_ms"], verify_before_send=True)
    done = bot.store.query_one("SELECT * FROM outbound_messages WHERE send_id=?", (row["send_id"],))
    assert done["state"] == "delivered"
    assert flaky.calls[-1][2] is True
