from __future__ import annotations

import html

from workbot.conversation.models import IncomingMessage
from workbot.im.welink import WeLinkAdapter
from workbot.main import WorkBot

OWNER = "example-user-b"
GROUP = "1001"


def _adapter() -> WeLinkAdapter:
    return WeLinkAdapter("welink-cli", [], self_accounts=[OWNER])


def _group_event(content: str, *, show_text: str = "", msg_id: str = "img-1") -> dict:
    inner = f"<imbody><content><![CDATA[{content}]]></content><html></html></imbody>"
    data = {
        "chatType": 1,
        "groupId": GROUP,
        "groupName": "Example Group",
        "userAccount": "sample-user-1",
        "senderNativeName": "Test User One",
        "serverSendTime": "1000",
        "msgId": msg_id,
        "content": f"<r><n>x</n><g>0</g><c>{html.escape(inner)}</c></r>",
    }
    if show_text:
        data["showText"] = show_text
    return {"type": "weLinkMessage", "func": "receiveIMMessage", "data": data}


def test_um_image_placeholder_is_media_only_not_agent_text():
    adapter = _adapter()
    marker = "/:um_begin{|Img|cdn=abc123;width=640;height=480}/:um_end"
    msg = adapter.normalize_push_event(_group_event(marker))
    assert msg is not None
    assert msg.media_only is True
    assert msg.media_types == ("image",)
    assert msg.content == "[图片]"
    assert "um_begin" not in msg.content


def test_media_plus_human_caption_keeps_only_caption():
    adapter = _adapter()
    marker = "/:um_begin{|Img|cdn=abc123}/:um_end"
    msg = adapter.normalize_push_event(_group_event(f"这个日志截图有问题吗？ {marker}", msg_id="img-2"))
    assert msg is not None
    assert msg.media_only is False
    assert msg.media_types == ("image",)
    assert msg.content == "这个日志截图有问题吗？"


def test_showtext_media_placeholder_is_removed():
    adapter = _adapter()
    marker = "/:um_begin{|Img|foo=bar}/:um_end"
    msg = adapter.normalize_push_event(_group_event(marker, show_text=marker, msg_id="img-3"))
    assert msg is not None
    assert msg.media_only
    assert msg.content == "[图片]"


def test_filelist_without_text_is_store_only_media():
    adapter = _adapter()
    event = {
        "type": "weLinkMessage",
        "func": "receiveIMMessage",
        "data": {
            "chatType": 0,
            "isMine": False,
            "receiverAccount": OWNER,
            "recentOwner": "u2",
            "userAccount": "sample-user-2",
            "senderNativeName": "Test User Two",
            "serverSendTime": "2000",
            "id": "local-file-1",
            "content": "",
            "fileList": [{"name": "screen.png", "mimeType": "image/png"}],
        },
    }
    msg = adapter.normalize_push_event(event)
    assert msg is not None
    assert msg.media_only
    assert msg.media_types == ("image",)
    assert msg.content == "[图片]"


def test_dispatch_media_only_never_enters_agent_pipeline():
    bot = object.__new__(WorkBot)
    bot._reply_allowed = lambda msg: True
    bot._execution_allowed = lambda msg: True

    touched: list[str] = []
    bot._enqueue_conversation_message = lambda msg: touched.append("enqueue")
    bot._spawn = lambda *args, **kwargs: touched.append("spawn")

    msg = IncomingMessage(
        platform="welink",
        conversation_id=f"welink:group:{GROUP}",
        conversation_kind="group",
        external_conversation_id=GROUP,
        external_message_id="media-1",
        sender_id="sample-user-1",
        content="[图片]",
        sent_at_ms=1000,
        transport="welinkbot",
        media_only=True,
        media_types=("image",),
    )
    bot._dispatch_im_message(msg)
    assert touched == []


def test_history_text_media_placeholder_can_be_classified_without_agent_text():
    adapter = _adapter()
    visible, media_types = adapter._strip_media_embeds("/:um_begin{|Img|history-image}/:um_end")
    assert visible == ""
    assert media_types == ("image",)
