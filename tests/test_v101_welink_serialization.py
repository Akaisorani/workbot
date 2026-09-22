import asyncio
import time

import pytest

from workbot.im.welink import WeLinkAdapter


class SerialProbeWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__(
            "welink-cli",
            [{"group_id": "g1", "group_name": "g1"}],
            bootstrap_from_latest=False,
            send_verify_delay_seconds=0,
        )
        self.active = 0
        self.max_active = 0

    def _enter_probe(self):
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def _leave_probe(self):
        self.active -= 1

    def _poll_sync(self):
        self._enter_probe()
        try:
            time.sleep(0.05)
            return []
        finally:
            self._leave_probe()

    def _send_group_once(self, group_id: str, text: str):
        self._enter_probe()
        try:
            time.sleep(0.05)
        finally:
            self._leave_probe()


@pytest.mark.asyncio
async def test_poll_send_and_retry_share_one_operation_gate():
    a = SerialProbeWeLink()
    await asyncio.gather(
        a.poll(),
        a.send_text("welink:group:g1", "hello"),
        a.send_text("welink:group:g1", "world"),
    )
    assert a.max_active == 1


def test_cli_trace_redacts_message_payload():
    a = WeLinkAdapter("welink-cli", [{"group_id": "g1"}], bootstrap_from_latest=False)
    cmd = a._display_command(("im", "send-to-group", "--group-id", "g1", "--text", "secret body"))
    assert "secret body" not in cmd
    assert "<text len=11 sha256=" in cmd
    assert "send-to-group" in cmd
