from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.im.rich_message import ImageContextProcessor
from workbot.im.welink import WeLinkAdapter

OWNER = "example-user-b"
GROUP = "1103"


def _adapter(tmp_path: Path, monkeypatch) -> WeLinkAdapter:
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    return WeLinkAdapter("welink-cli", [], self_accounts=[OWNER])


def _mobile_card_event(reply: str, *, quoted: str = "bot 你好", sender: str = OWNER):
    import html, json
    card = {
        "cardContext": {
            "preMsg": {
                "content": quoted,
                "customizable": {},
                "messageID": "101",
                "nameEN": "Test User",
                "nameZH": "测试用户",
                "sender": sender,
                "type": 0,
            },
            "replyMsg": {"content": reply, "type": 0},
        },
        "cardType": 65,
        "isShowSource": 0,
    }
    payload = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    content = (
        "<r><n></n><g>0</g><c>"
        + html.escape("<imbody><imagelist/><html></html><content><![CDATA[" + payload + "]]></content></imbody>")
        + "</c></r>"
    )
    return {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {
            "appId": "1", "chatContentType": 10, "chatType": 1,
            "content": content, "groupId": GROUP, "groupName": "Example Group",
            "userAccount": OWNER, "senderName": "Test User", "senderNativeName": "测试用户",
            "serverSendTime": "1001", "msgId": "102",
        },
    }


def test_mobile_quote_card_extracts_reply_and_quote(monkeypatch, tmp_path: Path):
    adapter = _adapter(tmp_path, monkeypatch)
    msg = adapter.normalize_push_event(_mobile_card_event("bot test quote from mobile"))
    assert msg is not None
    assert msg.content == "bot test quote from mobile"
    assert msg.quote is not None
    assert msg.quote["content"] == "bot 你好"
    assert msg.quote["message_id"] == "101"
    assert msg.quote["sender_id"] == OWNER


def test_mobile_quote_card_keeps_bot_alias_visible_to_alias_gate(monkeypatch, tmp_path: Path):
    adapter = _adapter(tmp_path, monkeypatch)
    msg = adapter.normalize_push_event(_mobile_card_event("bot 帮我为本地代理安装Example_Plugin", quoted="agent plugin add Example_Plugin@latest 最新版本应该是1.0.0", sender="example-user-a"))
    assert msg is not None
    # The normalizer must expose the current reply text, not the JSON envelope.
    assert msg.content.startswith("bot ")
    assert msg.quote and msg.quote["sender_id"] == "example-user-a"


@pytest.mark.asyncio
async def test_existing_image_remains_multimodal_context_when_ocr_fails(tmp_path: Path):
    root = tmp_path / "ReceiveFiles"
    root.mkdir()
    image = root / "cat.png"
    image.write_bytes(b"fake")
    processor = ImageContextProcessor({
        "image": {"enabled": True, "allowed_roots": [str(root)], "path_wait_seconds": 0,
                  "ocr": {"enabled": True, "provider": "rapidocr", "timeout_seconds": 2}}
    })
    msg = IncomingMessage("welink", "welink:group:g", "group", "g", "1", "u", "bot 这张图是什么", 1,
                          image_paths=(str(image),))
    with mock.patch.object(processor, "_ocr_path", side_effect=RuntimeError("ocr broken")):
        enriched = await processor.enrich(msg)
    item = enriched.image_context[0]
    assert item["status"] == "available"
    assert "OCR failed" in item["reason"]
    prompt_context = ConversationManager.format_context_for_agent({"image_context": list(enriched.image_context)})
    assert "direct multimodal inspection" in prompt_context
    assert str(image) in prompt_context


def test_image_ocr_is_supplement_not_replacement(tmp_path: Path):
    image = tmp_path / "screen.png"
    context = ConversationManager.format_context_for_agent({
        "image_context": [{"path": str(image), "status": "ok", "text": "ERROR 123"}]
    })
    assert "direct multimodal inspection" in context
    assert "OCR supplement" in context
    assert "ERROR 123" in context
