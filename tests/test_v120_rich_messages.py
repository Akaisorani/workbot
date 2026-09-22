from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from workbot.conversation.models import IncomingMessage
from workbot.im.rich_message import ImageContextProcessor
from workbot.im.welink import WeLinkAdapter
from workbot.main import WorkBot

OWNER = "example-user-b"
GROUP = "1103"


def _adapter(tmp_path: Path, monkeypatch) -> WeLinkAdapter:
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    return WeLinkAdapter("welink-cli", [], self_accounts=[OWNER])


def test_group_quote_payload_is_normalized(monkeypatch, tmp_path: Path):
    a = _adapter(tmp_path, monkeypatch)
    event = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {
            "showText": "bot 处理这个", "chatType": 1, "groupId": GROUP,
            "userAccount": OWNER, "senderNativeName": "测试用户甲", "serverSendTime": "1000",
            "clientMsgId": "m1", "isMine": True,
            "quote": {
                "content": "bot 在 linux-server1 拉一下 example_repo 最新代码然后编译",
                "showText": "bot 在 linux-server1 拉一下 example_repo 最新代码然后编译",
                "messageID": "103", "nameZH": "测试用户乙",
                "sender": "example-user-a", "type": 0, "chatContentType": 0, "fileList": [],
            },
        },
    }
    msg = a.normalize_push_event(event)
    assert msg is not None
    assert msg.content == "bot 处理这个"
    assert msg.quote == {
        "message_id": "103",
        "sender_id": "example-user-a",
        "sender_name": "测试用户乙",
        "content": "bot 在 linux-server1 拉一下 example_repo 最新代码然后编译",
        "type": 0,
        "chat_content_type": 0,
        "image_paths": [],
    }


def test_private_quote_payload_is_normalized(monkeypatch, tmp_path: Path):
    a = _adapter(tmp_path, monkeypatch)
    event = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {
            "showText": "reply to example message2", "chatType": 0,
            "receiverAccount": "example-user-d", "recentOwner": "example-user-d",
            "userAccount": OWNER, "isMine": True, "serverSendTime": "2000", "id": "x",
            "quote": {"content": "example message2", "showText": "example message2",
                      "messageID": "104", "nameZH": "测试用户甲", "sender": OWNER, "type": 0},
        },
    }
    msg = a.normalize_push_event(event)
    assert msg is not None
    assert msg.conversation_id == "welink:user:example-user-d"
    assert msg.from_self is True
    assert msg.quote and msg.quote["content"] == "example message2"
    assert msg.quote["sender_id"] == OWNER


def test_own_image_uses_file_real_save_path_and_new_field_names(monkeypatch, tmp_path: Path):
    a = _adapter(tmp_path, monkeypatch)
    screenshot = tmp_path / "AppData" / "Roaming" / "WeLink_Desktop" / "appdata" / "IM" / OWNER / "ReceiveFiles" / "ScreenShot" / "shot.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"fake")
    event = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {
            "groupId": GROUP, "chatType": 1, "showText": "\n", "content": "/:um_begin{|Img|0|shot.png|0|157;116;|md5:x}/:um_end",
            "fileList": [{"file_type": "Img", "file_name": "shot.png", "file_real_save_path": str(screenshot)}],
            "userAccount": OWNER, "serverSendTime": "3000", "clientMsgId": "m3",
        },
    }
    msg = a.normalize_push_event(event)
    assert msg is not None
    assert msg.media_only is True
    assert "image" in msg.media_types
    assert msg.image_paths == (str(screenshot),)


def test_peer_image_filename_is_resolved_to_welink_screenshot_root(monkeypatch, tmp_path: Path):
    a = _adapter(tmp_path, monkeypatch)
    filename = "sample-image.gif"
    expected = tmp_path / "AppData" / "Roaming" / "WeLink_Desktop" / "appdata" / "IM" / OWNER / "ReceiveFiles" / "ScreenShot" / filename
    event = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {
            "groupId": GROUP, "chatType": 1, "userAccount": "example-image-user", "serverSendTime": "4000", "msgId": "m4",
            "content": f"/:um_begin{{https://example.invalid/f/x|Img|85485|{filename}|0|566;53;abc|md5:x}}/:um_end",
        },
    }
    msg = a.normalize_push_event(event)
    assert msg is not None
    assert msg.media_only is True
    assert msg.image_paths == (str(expected),)


@pytest.mark.asyncio
async def test_image_ocr_success_and_failure_are_nonfatal(tmp_path: Path):
    root = tmp_path / "ReceiveFiles"
    root.mkdir()
    image = root / "shot.png"
    image.write_bytes(b"fake")
    processor = ImageContextProcessor({
        "image": {"enabled": True, "allowed_roots": [str(root)], "path_wait_seconds": 0,
                  "ocr": {"enabled": True, "provider": "rapidocr", "timeout_seconds": 2}}
    })
    msg = IncomingMessage("welink", "welink:group:g", "group", "g", "1", "u", "bot 看图", 1,
                          image_paths=(str(image), str(root / "missing.png")))
    with mock.patch.object(processor, "_ocr_path", return_value=("错误码 123\n编译失败", 0.95)):
        enriched = await processor.enrich(msg)
    assert enriched.image_context[0]["status"] == "ok"
    assert "编译失败" in enriched.image_context[0]["text"]
    assert enriched.image_context[1]["status"] == "unavailable"
    assert "not found" in enriched.image_context[1]["reason"]
    assert enriched.content == "bot 看图"


class FakeIM:
    def __init__(self):
        self.sent = []
    async def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))


def _bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {"type": "welink", "groups": [], "intent": {"require_alias": False, "bot_aliases": ["bot"]},
               "rich_message": {"image": {"enabled": False}}},
        "execution_policy": {"default": "deny", "allow_senders": [OWNER]},
        "reply_policy": {"default": "allow", "silent_if_unfulfillable": True},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "default_node": "linux-server1",
        "nodes": {"linux-server1": {"ssh_alias": "linux-server1", "relay_command": "noop"}},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = FakeIM()
    return bot


@pytest.mark.asyncio
async def test_quote_handle_this_becomes_remote_task_proposal_with_provenance(tmp_path: Path):
    bot = _bot(tmp_path)
    msg = IncomingMessage(
        "welink", "welink:group:g", "group", "g", "200", OWNER, "处理这个", 200, "g",
        quote={
            "message_id": "100", "sender_id": "example-user-a", "sender_name": "测试用户乙",
            "content": "bot 在 linux-server1 拉一下 example_repo 最新代码然后编译", "image_paths": [],
        },
    )
    await bot.handle_im_message(msg)
    pending = bot.conversations.get_pending_action(msg.conversation_id)
    assert pending is not None and pending.action_type == "task.create"
    assert pending.payload["node"] == "linux-server1"
    assert pending.payload["instruction"] == "在 linux-server1 拉一下 example_repo 最新代码然后编译"
    assert pending.payload["requested_by"] == "example-user-a"
    assert pending.payload["authorized_by"] == OWNER
    assert pending.payload["origin_message_id"] == "100"
    assert pending.payload["authorization_message_id"] == "200"


@pytest.mark.asyncio
async def test_quote_summary_remains_agent_context_not_execution(tmp_path: Path):
    bot = _bot(tmp_path)
    msg = IncomingMessage(
        "welink", "welink:group:g", "group", "g", "201", OWNER, "总结这个", 201, "g",
        quote={"message_id": "100", "sender_id": "example-user-a", "content": "在 linux-server1 编译 example_repo", "image_paths": []},
    )
    class Result:
        text = "总结结果"
        action = None
        approval = None
    async def fake_answer(conversation_id, text):
        assert "总结这个" in text
        assert "Quoted message" in text
        assert "在 linux-server1 编译 example_repo" in text
        return Result()
    bot.agents.answer = fake_answer
    await bot.handle_im_message(msg)
    assert bot.conversations.get_pending_action(msg.conversation_id) is None
    assert bot.im.sent[-1][1] == "总结结果"


def test_old_database_migrates_messages_context_json(tmp_path: Path):
    # The Store migration is covered indirectly by construction; assert the
    # current schema exposes context_json for pre-existing-compatible DBs.
    bot = _bot(tmp_path)
    cols = [r[1] for r in bot.store.query_all("PRAGMA table_info(messages)")]
    assert "context_json" in cols


def test_release_vision_setup_and_readme_convention_present():
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.12.4"' in pyproject
    assert 'vision = ["rapidocr>=3,<4"' in pyproject
    assert (root / "README.md").exists()
    assert (root / "README_EN.md").exists()
    assert not (root / "README_ZH.md").exists()
    setup = (root / "scripts" / "setup-vision.ps1").read_text(encoding="utf-8")
    assert 'pip install -e ".[vision]"' in setup
    assert "WORKBOT_VISION_SETUP_OK" in setup


def test_store_migrates_legacy_messages_table_with_context_json(tmp_path: Path):
    import sqlite3
    from workbot.storage.sqlite import Store

    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      conversation_id TEXT NOT NULL,
      external_message_id TEXT NOT NULL,
      sender_id TEXT,
      direction TEXT NOT NULL,
      content TEXT NOT NULL,
      sent_at INTEGER,
      transport TEXT,
      dedup_key TEXT,
      content_key TEXT,
      created_at INTEGER NOT NULL DEFAULT (unixepoch()),
      UNIQUE(conversation_id, external_message_id)
    );
    """)
    conn.commit(); conn.close()
    store = Store(db)
    cols = [r[1] for r in store.query_all("PRAGMA table_info(messages)")]
    assert "context_json" in cols
