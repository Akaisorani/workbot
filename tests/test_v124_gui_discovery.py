from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from workbot.collaboration.protocol import (
    AgentEnvelope,
    decode_agent_message,
    encode_agent_message,
    encode_discovery_body,
)
from workbot.control.service import WorkBotControlService
from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot
from workbot.setup.configurator import validate_config


class FakeIM:
    def __init__(self):
        self.sent = []

    async def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))


def make_bot(tmp_path: Path, collaboration=None):
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {
            "cli": "welink-cli",
            "groups": [{"group_id": "g", "group_name": "agents"}],
            "self_accounts": ["example-user-b"],
            "intent": {"require_alias": True, "bot_aliases": ["WorkBot-A"]},
        },
        "ingestion_policy": {"default": "allow"},
        "reply_policy": {"default": "allow"},
        "execution_policy": {"default": "deny", "allow_senders": ["example-user-b"]},
        "agent": {"command": "codeagent"},
        "nodes": {},
        "rag": {"enabled": False},
        "collaboration": collaboration or {
            "enabled": True,
            "agent_id": "bot-a",
            "groups": ["g"],
            "discovery_enabled": True,
            "peers": {},
        },
    }
    path = tmp_path / "config" / "local.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(path)
    bot.im = FakeIM()
    return bot


def incoming(text: str, sender: str, mid="m1"):
    return IncomingMessage(
        platform="welink",
        conversation_id="welink:group:g",
        conversation_kind="group",
        external_conversation_id="g",
        external_message_id=mid,
        sender_id=sender,
        content=text,
        sent_at_ms=1,
        display_name=sender,
        transport="test",
    )


@pytest.mark.asyncio
async def test_passive_discovery_hello_binds_sender_and_acks_once(tmp_path: Path):
    bot = make_bot(tmp_path)
    hello = encode_agent_message(
        AgentEnvelope(1, "bot-b", "*", "event", "amsg-hello", None, 0),
        encode_discovery_body(
            "hello", agent_id="bot-b", display_name="WorkBot-B",
            aliases=["WorkBot-B"], capabilities=["chat", "codeagent"],
        ),
    )
    recognized, routed = bot._route_collaboration_message(incoming("[AGENT-B]" + hello, "example-user-a"))
    assert recognized and routed is None
    await asyncio.sleep(0)
    peer = bot._collaboration_peer_config("bot-b")
    assert peer is not None
    assert peer["source"] == "discovered"
    assert peer["sender_accounts"] == ["example-user-a"]
    assert peer["display_name"] == "WorkBot-B"
    assert bot.im.sent, "hello should receive a targeted hello_ack"
    parsed = decode_agent_message(bot.im.sent[-1][1])
    assert parsed is not None
    env, body = parsed
    assert env.from_id == "bot-a" and env.to_id == "bot-b" and env.message_type == "event"
    assert env.reply_to == "amsg-hello"
    assert json.loads(body)["kind"] == "hello_ack"


@pytest.mark.asyncio
async def test_manual_discover_command_sends_one_hello_in_current_group(tmp_path: Path):
    bot = make_bot(tmp_path)
    handled = await bot._handle_lifecycle_command(incoming("/agent discover", "example-user-b"), "/agent discover")
    assert handled
    assert len(bot.im.sent) == 2  # one protocol hello plus one human confirmation
    assert bot.im.sent[0][0] == "welink:group:g"
    parsed = decode_agent_message(bot.im.sent[0][1])
    assert parsed is not None
    envelope, body = parsed
    assert envelope.to_id == "*" and envelope.message_type == "event"
    assert json.loads(body)["kind"] == "hello"
    assert decode_agent_message(bot.im.sent[1][1]) is None
    assert bot._collaboration_last_discovery_at is not None


@pytest.mark.asyncio
async def test_runtime_start_does_not_schedule_periodic_discovery(tmp_path: Path):
    bot = make_bot(tmp_path)
    spawned = []

    def capture(coro, *, name):
        spawned.append(name)
        coro.close()

    bot._spawn = capture
    bot.ssh.start = AsyncMock()
    bot.ssh.stop = AsyncMock()
    bot._recover_workflows = AsyncMock()
    bot._run_polling_im_loop = AsyncMock()
    await bot.run()
    assert "collaboration-discovery" not in spawned
    assert bot.im.sent == []


@pytest.mark.asyncio
async def test_discovered_peer_request_is_accepted_without_static_peers(tmp_path: Path):
    bot = make_bot(tmp_path)
    hello = encode_agent_message(
        AgentEnvelope(1, "bot-b", "*", "event", "amsg-h", None, 0),
        encode_discovery_body("hello", agent_id="bot-b"),
    )
    bot._route_collaboration_message(incoming(hello, "example-user-a", "1"))
    await asyncio.sleep(0)
    req = encode_agent_message(
        AgentEnvelope(1, "bot-b", "bot-a", "request", "amsg-r", None, 0),
        "帮我复核 patch",
    )
    recognized, routed = bot._route_collaboration_message(incoming(req, "example-user-a", "2"))
    assert recognized and routed is not None
    assert routed.agent_peer_id == "bot-b"
    assert routed.content == "帮我复核 patch"


def test_discovery_conflict_does_not_replace_live_sender_binding(tmp_path: Path):
    bot = make_bot(tmp_path)
    env = AgentEnvelope(1, "bot-b", "*", "event", "amsg-1", None, 0)
    body = encode_discovery_body("hello", agent_id="bot-b")
    assert bot._register_discovered_peer(incoming("", "example-user-a"), env, json.loads(body))
    env2 = AgentEnvelope(1, "bot-b", "*", "event", "amsg-2", None, 0)
    assert not bot._register_discovered_peer(incoming("", "example-user-c", "2"), env2, json.loads(body))
    assert bot._collaboration_peer_config("bot-b")["sender_accounts"] == ["example-user-a"]


def test_collaboration_validator_allows_manual_discovery_without_peers_or_agent_id():
    cfg = {
        "im": {"self_accounts": ["example-user-b"], "groups": [{"group_id": "g"}], "intent": {"bot_aliases": ["WorkBot"]}},
        "collaboration": {"enabled": True, "agent_id": "", "discovery_enabled": True, "peers": {}},
    }
    assert validate_config(cfg) == []


@pytest.mark.asyncio
async def test_control_service_snapshot_uses_authoritative_runtime_state(tmp_path: Path):
    bot = make_bot(tmp_path, collaboration={"enabled": False})
    service = WorkBotControlService(bot)
    snap = await service.snapshot()
    assert snap["runtime"]["config_path"].endswith("local.json")
    assert snap["scheduler"]["active"] == 0
    assert snap["tasks"] == []
    assert snap["workflows"] == []
    assert snap["collaboration"]["enabled"] is False


@pytest.mark.asyncio
async def test_control_service_manual_discovery_updates_snapshot(tmp_path: Path):
    bot = make_bot(tmp_path)
    service = WorkBotControlService(bot)
    result = await service.action("collaboration.discover")
    assert len(result["sent"]) == 1
    assert len(bot.im.sent) == 1
    snap = await service.snapshot()
    assert snap["collaboration"]["discovery_enabled"] is True
    assert snap["collaboration"]["last_discovery_at"] is not None


def test_gui_module_imports_without_constructing_window():
    # Headless release tests should be able to import the desktop frontend;
    # actual Tk window construction is intentionally left to the user's desktop.
    from workbot.gui import app
    assert callable(app.main)
    assert "ingestion_policy" in app.HOT_RELOAD_SECTIONS
