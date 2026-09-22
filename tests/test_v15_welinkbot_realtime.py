from __future__ import annotations

import asyncio
import json

import pytest

from workbot.im.welink import WeLinkAdapter
from workbot.im.welinkbot import WeLinkBotReceiver


OWNER = "example-user-b"


def _adapter() -> WeLinkAdapter:
    return WeLinkAdapter("welink-cli", [], self_accounts=[OWNER])


def _group_event(*, text: str, sender: str = "example-user-a", msg_id: str = "107", is_at: int = 0):
    escaped = (
        "<r><n>Test User</n><g>0</g><c>"
        "&lt;imbody&gt;&lt;html&gt;&lt;![CDATA[&lt;FONT&gt;" + text + "&lt;/FONT&gt;]]&gt;&lt;/html&gt;"
        "&lt;content&gt;" + text + "&lt;/content&gt;&lt;/imbody&gt;</c></r>"
    )
    return {
        "type": "weLinkMessage",
        "func": "receiveIMMessage",
        "data": {
            "chatContentType": 0,
            "chatType": 1,
            "content": escaped,
            "groupId": "example-group",
            "groupName": "Example Engineering Group",
            "isAt": is_at,
            "msgId": msg_id,
            "serverSendTime": "1001",
            "senderName": "Test User",
            "senderNativeName": "Test User",
            "userAccount": sender,
        },
    }


def test_realtime_group_message_parses_sample_hook_shape():
    adapter = _adapter()
    text = "for issue 113, why do you use force_stream_parameter_passing =on?"
    msg = adapter.normalize_push_event(_group_event(text=text, is_at=1))
    assert msg is not None
    assert msg.conversation_id == "welink:group:example-group"
    assert msg.external_message_id == "107"
    assert msg.sender_id == "example-user-a"
    assert msg.content == text
    assert msg.is_at is True
    assert msg.from_self is False
    assert msg.transport == "welinkbot"


def test_realtime_group_owner_command_is_not_misclassified_as_self():
    adapter = _adapter()
    event = _group_event(text="/approve approval-abcdef123456", sender=OWNER, msg_id="108")
    msg = adapter.normalize_push_event(event)
    assert msg is not None
    assert msg.sender_id == OWNER
    assert msg.from_self is False


def test_realtime_workbot_group_echo_is_suppressed():
    adapter = _adapter()
    event = _group_event(text="[自动回复]已批准。", sender=OWNER, msg_id="109")
    event["data"]["isAppMsg"] = 1
    assert adapter.normalize_push_event(event) is None


def test_realtime_private_is_mine_uses_receiver_as_peer_and_is_self():
    adapter = _adapter()
    event = {
        "type": "weLinkMessage",
        "func": "receiveIMMessage",
        "data": {
            "isAutoReply": 0,
            "content": "<r><n>x</n><g>0</g><c>&lt;imbody&gt;&lt;content&gt;&lt;![CDATA[test message]]&gt;&lt;/content&gt;&lt;/imbody&gt;</c></r>",
            "clientMsgId": "sample-client-1",
            "showText": "test message",
            "receiverAccount": "example-user-c",
            "recentOwner": "example-user-c",
            "userAccount": OWNER,
            "chatType": 0,
            "senderName": "Test User",
            "serverSendTime": "1002",
            "id": "local-1",
            "isMine": True,
        },
    }
    msg = adapter.normalize_push_event(event)
    assert msg is not None
    assert msg.conversation_id == "welink:user:example-user-c"
    assert msg.external_message_id == "client:sample-client-1"
    assert msg.from_self is True
    assert msg.content == "test message"


def test_request_backfill_pulls_known_conversations_due():
    adapter = _adapter()
    msg = adapter.normalize_push_event(_group_event(text="hello"))
    assert msg is not None
    conv = adapter._conversations["group:example-group"]
    conv.next_poll_at = 999999999.0
    adapter.request_backfill()
    assert conv.next_poll_at < 999999999.0


@pytest.mark.asyncio
async def test_welinkbot_receiver_auth_and_push_round_trip():
    websockets = pytest.importorskip("websockets")
    adapter = _adapter()
    stop = asyncio.Event()
    received_auth = {}

    async def handler(ws):
        received_auth.update(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "auth", "success": True}))
        await ws.send(json.dumps(_group_event(text="realtime hello", msg_id="110")))
        await stop.wait()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    receiver = WeLinkBotReceiver(
        {
            "enabled": True,
            "url": f"ws://127.0.0.1:{port}",
            "secret": "test-secret",
            "open_timeout_seconds": 2,
            "auth_timeout_seconds": 2,
            "reconnect_initial_seconds": 0.2,
        },
        adapter.normalize_push_event,
    )
    task = asyncio.create_task(receiver.run(stop))
    try:
        batch = await receiver.get_batch(timeout=2)
        assert len(batch) == 1
        assert batch[0].content == "realtime hello"
        assert received_auth == {"type": "auth", "secret": "test-secret"}
        assert receiver.connected
        assert receiver.backfill_generation == 1
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()

def test_realtime_ingested_dm_advances_time_cursor_without_numeric_msgid():
    adapter = _adapter()
    event = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {
            "showText": "incoming dm", "chatType": 0,
            "userAccount": "example-user-c", "receiverAccount": OWNER,
            "serverSendTime": "1003", "clientMsgId": "peer-1", "isMine": False,
        },
    }
    msg = adapter.normalize_push_event(event)
    assert msg is not None
    adapter.note_ingested_message(msg)
    assert adapter._last_seen_time_ms["user:example-user-c"] == 1003
    assert "user:example-user-c" in adapter._bootstrapped
