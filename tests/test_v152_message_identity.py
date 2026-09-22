from __future__ import annotations

import json
from pathlib import Path

from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.im.welink import WeLinkAdapter
from workbot.policy.approvals import ApprovalManager
from workbot.storage.sqlite import Store

OWNER = "example-user-b"
GROUP = "1001"


def _adapter() -> WeLinkAdapter:
    return WeLinkAdapter("welink-cli", [], self_accounts=[OWNER])


def _hook_group(text: str, *, local_id: str = "hook-local-1", ts: int = 1000):
    inner = f"<imbody><content><![CDATA[{text}]]></content><html></html></imbody>"
    import html
    return {
        "type": "weLinkMessage",
        "func": "receiveIMMessage",
        "data": {
            "chatType": 1,
            "groupId": GROUP,
            "groupName": "Example Group",
            "userAccount": OWNER,
            "senderName": "Test User",
            "senderNativeName": "测试用户",
            "serverSendTime": str(ts),
            # Simulate the Hook quirk: no stable msgId, only a local id.
            "id": local_id,
            "content": f"<r><n>x</n><g>0</g><c>{html.escape(inner)}</c></r>",
        },
    }


def test_hook_and_history_dedup_even_when_external_ids_differ(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    conversations = ConversationManager(store)
    adapter = _adapter()
    text = "/approve approval-fbe594171a48"
    push = adapter.normalize_push_event(_hook_group(text))
    assert push is not None
    assert push.external_message_id.startswith("local:")
    assert conversations.add_incoming(push) is True

    history = IncomingMessage(
        platform="welink",
        conversation_id=f"welink:group:{GROUP}",
        conversation_kind="group",
        external_conversation_id=GROUP,
        external_message_id="105",
        sender_id=OWNER,
        content=text,
        sent_at_ms=push.sent_at_ms,
        display_name="Example Group",
        transport="welink-cli-history",
        dedup_key=adapter._logical_message_key(f"group:{GROUP}", OWNER, text, push.sent_at_ms),
    )
    assert history.external_message_id != push.external_message_id
    assert history.dedup_key == push.dedup_key
    assert conversations.add_incoming(history) is False
    assert conversations.message_count(f"welink:group:{GROUP}") == 1


def test_cross_transport_timestamp_skew_has_narrow_fuzzy_dedup(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    conversations = ConversationManager(store)
    adapter = _adapter()
    text = "bot 创建日历 今天下午6点-7点 测试日程2"
    push = adapter.normalize_push_event(_hook_group(text, ts=1000))
    assert push is not None
    assert conversations.add_incoming(push)

    # Different timestamp => different deterministic fingerprint, but a 1.2 s
    # Hook/API skew for the same sender/content should still collapse.
    history_ts = push.sent_at_ms + 1200
    history = IncomingMessage(
        "welink", f"welink:group:{GROUP}", "group", GROUP,
        "106", OWNER, text, history_ts, "Example Group",
        transport="welink-cli-history",
        dedup_key=adapter._logical_message_key(f"group:{GROUP}", OWNER, text, history_ts),
    )
    assert history.dedup_key != push.dedup_key
    assert conversations.add_incoming(history) is False


def test_same_transport_intentional_repeat_is_not_fuzzy_collapsed(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    conversations = ConversationManager(store)
    adapter = _adapter()
    text = "OK"
    a = adapter.normalize_push_event(_hook_group(text, local_id="a", ts=1000))
    b = adapter.normalize_push_event(_hook_group(text, local_id="b", ts=2200))
    assert a and b
    assert a.dedup_key != b.dedup_key
    assert conversations.add_incoming(a) is True
    assert conversations.add_incoming(b) is True
    assert conversations.message_count(f"welink:group:{GROUP}") == 2


def test_duplicate_hook_delivery_with_changed_local_id_collapses(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    conversations = ConversationManager(store)
    adapter = _adapter()
    a = adapter.normalize_push_event(_hook_group("same event", local_id="local-a"))
    b = adapter.normalize_push_event(_hook_group("same event", local_id="local-b"))
    assert a and b and a.external_message_id != b.external_message_id
    assert a.dedup_key == b.dedup_key
    assert conversations.add_incoming(a) is True
    assert conversations.add_incoming(b) is False


def test_rich_json_card_extracts_text_without_exposing_protocol_blob():
    adapter = _adapter()
    event = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {
            "chatType": 1, "groupId": GROUP, "groupName": "Example Group",
            "userAccount": "sample-user", "serverSendTime": "1000", "msgId": "123",
            "content": json.dumps({"title": "构建报告", "body": {"text": "fastcheck 失败 2 项"}}, ensure_ascii=False),
        },
    }
    msg = adapter.normalize_push_event(event)
    assert msg is not None
    assert "构建报告" in msg.content
    assert "fastcheck 失败 2 项" in msg.content
    assert not msg.content.startswith("{")


def test_dm_display_name_upgrades_after_peer_replies():
    adapter = _adapter()
    mine = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {"chatType": 0, "isMine": True, "receiverAccount": "example-peer", "recentOwner": "example-peer",
                 "userAccount": OWNER, "showText": "hello", "serverSendTime": "1000", "id": "1"},
    }
    msg1 = adapter.normalize_push_event(mine)
    assert msg1 and msg1.display_name == "example-peer"
    incoming = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {"chatType": 0, "isMine": False, "receiverAccount": OWNER, "recentOwner": "example-peer",
                 "userAccount": "example-peer", "senderNativeName": "Example User", "showText": "hi", "serverSendTime": "2000", "id": "2"},
    }
    msg2 = adapter.normalize_push_event(incoming)
    assert msg2 and msg2.display_name == "Example User"


def test_approval_decision_and_resume_are_idempotent(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    mgr = ApprovalManager(store, {"approver_senders": [OWNER]})
    aid = mgr.create(
        conversation_id=f"welink:group:{GROUP}", node="office-pc", task_id=None,
        action="calendar.write", summary="create event",
        details={"_resume_kind": "conversation"}, requested_by="windows-codeagent",
    )
    mgr.mark_resume_pending(aid)
    first = mgr.decide(aid, approved=True, sender_id=OWNER)
    second = mgr.decide(aid, approved=True, sender_id=OWNER)
    assert first.decision == second.decision == "approved"
    assert first.changed is True
    assert second.changed is False
    assert mgr.claim_resume(aid) is True
    assert mgr.claim_resume(aid) is False
    mgr.finish_resume(aid, success=True)
    row = mgr.get(aid)
    assert row["resume_state"] == "completed"

import asyncio
from unittest import mock
from workbot.main import WorkBot


class _FakeIM:
    def __init__(self):
        self.sent = []
    async def send_text(self, cid, text):
        self.sent.append((cid, text))


def _make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {"cli": "welink-cli", "groups": [], "bootstrap_from_latest": True},
        "agent": {"command": "codeagent"},
        "nodes": {},
        "access_control": {"default": "deny", "allow_senders": [OWNER]},
        "action_policy": {"approver_senders": [OWNER], "approval_timeout_seconds": 5},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = _FakeIM()
    return bot


async def _drain():
    for _ in range(20):
        await asyncio.sleep(0.01)


import pytest


@pytest.mark.asyncio
async def test_duplicate_approve_command_does_not_spawn_second_resume(tmp_path: Path):
    bot = _make_bot(tmp_path)
    aid = bot.approvals.create(
        conversation_id=f"welink:group:{GROUP}", node="office-pc", task_id=None,
        action="calendar.write", summary="测试日程2",
        details={"_resume_kind": "conversation"}, requested_by="windows-codeagent",
    )
    bot.approvals.mark_resume_pending(aid)
    resumes = []

    async def fake_resume(row):
        resumes.append(row["approval_id"])

    bot._resume_conversation_after_approval = fake_resume
    first = IncomingMessage("welink", f"welink:group:{GROUP}", "group", GROUP, "m1", OWNER, f"/approve {aid}", 1, "Example Group")
    second = IncomingMessage("welink", f"welink:group:{GROUP}", "group", GROUP, "m2", OWNER, f"/approve {aid}", 2, "Example Group")
    assert await bot._handle_lifecycle_command(first, first.content)
    await _drain()
    assert resumes == [aid]
    assert await bot._handle_lifecycle_command(second, second.content)
    await _drain()
    assert resumes == [aid]
    assert any("不会重复执行" in text for _, text in bot.im.sent)
