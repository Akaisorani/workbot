from __future__ import annotations

import html
import json
from pathlib import Path
from unittest import mock

from workbot.conversation.models import IncomingMessage
from workbot.im.welink import WeLinkAdapter
from workbot.main import WorkBot

GROUP = "1101"
OWNER = "owner-account"
SAMPLE_IMAGE = (
    "/:um_begin{https://files.example.invalid/f/example-token|"
    "Img|191039|6955B32C-75FF-4464-A784-049F1E2B0759.png|0|753;788;297c739128b552068ba9|"
    "isOriginalImg: 0;md5:00000000000000000000000000000000;isCrossInstance:0;emotionId:;"
    "objectId:;cdnUrl:}/:um_end"
)


def _adapter() -> WeLinkAdapter:
    return WeLinkAdapter("welink-cli", [], self_accounts=[OWNER])


def _group_event(text: str, msg_id: str) -> dict:
    inner = f"<imbody><content><![CDATA[{text}]]></content><html></html></imbody>"
    return {
        "type": "weLinkMessage",
        "func": "receiveIMMessage",
        "data": {
            "chatType": 1,
            "groupId": GROUP,
            "groupName": "test",
            "userAccount": "peer-account",
            "senderNativeName": "User",
            "serverSendTime": "1001",
            "msgId": msg_id,
            "content": f"<r><n>x</n><g>0</g><c>{html.escape(inner)}</c></r>",
        },
    }


def _bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {
            "cli": "welink-cli",
            "groups": [],
            "bootstrap_from_latest": True,
            "intent": {"enabled": True, "require_alias": True, "bot_aliases": ["bot", "WorkBot"]},
        },
        "ingestion_policy": {"default": "allow"},
        "reply_policy": {"default": "allow", "silent_if_unfulfillable": True},
        "execution_policy": {"default": "deny", "allow_senders": [OWNER]},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "rpc": {"enabled": False, "windows_read_roots": [str(tmp_path)]},
        "workspace_knowledge": {"enabled": False},
        "nodes": {},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = mock.AsyncMock()
    return bot


def test_sample_url_prefixed_image_embed_is_removed_from_caption():
    adapter = _adapter()
    question = (
        "条件如果已经下推到stream线程了，result算子是不是不需要了，这里直接让subplan算子接streamoperator"
    )
    msg = adapter.normalize_push_event(_group_event(f"{SAMPLE_IMAGE}\n{question}", "sample-image-1"))
    assert msg is not None
    assert msg.media_only is False
    assert msg.media_types == ("image",)
    assert msg.content == question
    assert not msg.content.startswith("/")
    assert "um_begin" not in msg.content


def test_sample_image_then_unaddressed_text_is_store_only(tmp_path: Path):
    adapter = _adapter()
    question = "条件如果已经下推到stream线程了，result算子是不是不需要了"
    msg = adapter.normalize_push_event(_group_event(f"{SAMPLE_IMAGE}\n{question}", "sample-image-2"))
    assert msg is not None

    bot = _bot(tmp_path)
    queued = []
    bot._enqueue_conversation_message = lambda m: queued.append(m.content)
    bot._dispatch_im_message(msg)
    assert queued == []


def test_sample_image_before_alias_is_allowed(tmp_path: Path):
    adapter = _adapter()
    msg = adapter.normalize_push_event(_group_event(f"{SAMPLE_IMAGE}\n   bot 帮我分析这个问题", "sample-image-3"))
    assert msg is not None
    assert msg.media_types == ("image",)
    assert msg.content == "bot 帮我分析这个问题"

    bot = _bot(tmp_path)
    queued = []
    bot._enqueue_conversation_message = lambda m: queued.append(m.content)
    bot._dispatch_im_message(msg)
    assert queued == ["帮我分析这个问题"]


def test_alias_in_middle_of_sentence_does_not_trigger(tmp_path: Path):
    bot = _bot(tmp_path)
    queued = []
    bot._enqueue_conversation_message = lambda m: queued.append(m.content)
    msg = IncomingMessage(
        "welink", f"welink:group:{GROUP}", "group", GROUP, "sample-text-1", "peer-account",
        "没有bot也会了吗", 1002, "User", transport="welinkbot"
    )
    bot._dispatch_im_message(msg)
    assert queued == []


def test_leading_whitespace_then_alias_triggers(tmp_path: Path):
    bot = _bot(tmp_path)
    queued = []
    bot._enqueue_conversation_message = lambda m: queued.append(m.content)
    msg = IncomingMessage(
        "welink", f"welink:group:{GROUP}", "group", GROUP, "sample-text-2", "peer-account",
        " \t  WorkBot   看一下", 1003, "User", transport="welinkbot"
    )
    bot._dispatch_im_message(msg)
    assert queued == ["看一下"]


def test_known_slash_command_bypasses_alias_but_unknown_slash_does_not(tmp_path: Path):
    bot = _bot(tmp_path)
    direct = bot._alias_dispatch_message(IncomingMessage(
        "welink", f"welink:group:{GROUP}", "group", GROUP, "slash-1", OWNER,
        "   /status", 1, "Owner", transport="welinkbot"
    ))
    assert direct is not None
    assert direct.content == "/status"

    addressed = bot._alias_dispatch_message(IncomingMessage(
        "welink", f"welink:group:{GROUP}", "group", GROUP, "slash-2", OWNER,
        " bot /status", 2, "Owner", transport="welinkbot"
    ))
    assert addressed is not None
    assert addressed.content == "/status"

    for content in ("/foo", "/status extra", "/:um_begin{https://example/x|Img|1}/:um_end"):
        assert bot._alias_dispatch_message(IncomingMessage(
            "welink", f"welink:group:{GROUP}", "group", GROUP, "slash-x-" + str(len(content)), OWNER,
            content, 3, "Owner", transport="welinkbot"
        )) is None


def test_known_slash_command_whitelist_covers_parameterized_commands(tmp_path: Path):
    bot = _bot(tmp_path)
    valid = [
        "/sessions",
        "/memory stream pbe",
        "/remember global title | body",
        "/forget #12",
        "/workspace stream_pbe_sender",
        "/summary refresh",
        "/stop-sessions all",
        "/retry",
    ]
    assert all(bot._is_known_slash_command(x) for x in valid)
    invalid = ["/unknown", "/forget whatever", "/session set nope", "/approve nope", "/:um_begin"]
    assert not any(bot._is_known_slash_command(x) for x in invalid)
