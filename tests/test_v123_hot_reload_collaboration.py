from __future__ import annotations

import json
from pathlib import Path

import pytest

from workbot.agents.codeagent import CodeAgentBackend
from workbot.collaboration.protocol import AgentEnvelope, decode_agent_message, encode_agent_message
from workbot.conversation.models import IncomingMessage, PendingAction
from workbot.im.welink import WeLinkAdapter
from workbot.main import WorkBot
from workbot.setup.configurator import validate_config


class FakeIM:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, conversation_id: str, text: str):
        self.sent.append((conversation_id, text))


def make_bot(tmp_path: Path, *, collaboration: dict | None = None) -> tuple[WorkBot, Path]:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "config_reload_interval_seconds": 0.5,
        "im": {
            "cli": "welink-cli",
            "groups": [],
            "self_accounts": ["example-user-b"],
            "intent": {"require_alias": True, "bot_aliases": ["bot-a"]},
        },
        "ingestion_policy": {"default": "allow"},
        "reply_policy": {"default": "allow", "silent_if_unfulfillable": True},
        "execution_policy": {"default": "deny", "allow_senders": ["example-user-b"]},
        "agent": {"command": "codeagent"},
        "nodes": {},
        "collaboration": collaboration or {"enabled": False},
    }
    path = tmp_path / "config" / "local.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(path)
    bot.im = FakeIM()
    return bot, path


def incoming(text: str, *, sender: str = "example-user-c", mid: str = "1") -> IncomingMessage:
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


def test_hot_reload_policies_are_atomic_and_last_good_survives_invalid_edit(tmp_path: Path):
    bot, path = make_bot(tmp_path)
    probe = incoming("hello")
    assert bot.ingestion_policy.inbound(probe).allowed
    assert bot.reply_policy.inbound(probe).allowed
    assert not bot.policy.inbound(probe).allowed

    cfg = json.loads(path.read_text())
    cfg["ingestion_policy"] = {"default": "deny", "allow_senders": ["trusted"]}
    cfg["reply_policy"] = {"default": "deny", "allow_senders": ["trusted"], "silent_if_unfulfillable": False}
    cfg["execution_policy"] = {"default": "allow"}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    changed = bot._reload_hot_config_if_changed(force=True)
    assert set(changed) == {"ingestion_policy", "reply_policy", "execution_policy"}
    assert not bot.ingestion_policy.inbound(probe).allowed
    assert not bot.reply_policy.inbound(probe).allowed
    assert bot.policy.inbound(probe).allowed
    assert bot._silent_unfulfillable is False

    # One invalid policy rejects the entire policy reload and preserves all three.
    cfg["ingestion_policy"] = {"default": "allow"}
    cfg["reply_policy"] = {"default": "definitely-invalid"}
    cfg["execution_policy"] = {"default": "deny"}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    assert bot._reload_hot_config_if_changed(force=True) == []
    assert not bot.ingestion_policy.inbound(probe).allowed
    assert not bot.reply_policy.inbound(probe).allowed
    assert bot.policy.inbound(probe).allowed


def test_codeagent_default_prompt_args_and_example_are_safe(tmp_path: Path):
    backend = CodeAgentBackend({"command": "codeagent"}, tmp_path)
    assert backend.prompt_args == [
        "--skip-safe-check", "--permission-mode", "bypassPermissions", "-p", "{prompt}"
    ]
    root = Path(__file__).resolve().parents[1]
    example = json.loads((root / "config" / "workbot.example.json").read_text(encoding="utf-8"))
    assert example["agent"]["prompt_args"] == backend.prompt_args
    assert example["nodes"] == {}
    assert example["default_node"] is None


def test_generic_agent_prefix_only_suppresses_local_self_echo():
    adapter = WeLinkAdapter("welink-cli", [], self_accounts=["example-user-b"], reply_prefix="[AGENT]")
    assert adapter._is_bot_reply("group:g", "[AGENT]hello", sender_id="example-user-b")
    assert not adapter._is_bot_reply("group:g", "[AGENT]hello", sender_id="example-user-a")


def test_collaboration_protocol_roundtrip():
    env = AgentEnvelope(1, "bot-b", "bot-a", "request", "amsg-1", None, 0)
    text = "[AGENT]" + encode_agent_message(env, "请分析这个问题")
    parsed = decode_agent_message(text)
    assert parsed is not None
    got, body = parsed
    assert got == env
    assert body == "请分析这个问题"


def test_verified_peer_request_bypasses_alias_but_response_is_store_only(tmp_path: Path):
    collab = {
        "enabled": True,
        "agent_id": "bot-a",
        "groups": ["g"],
        "accept_broadcast": False,
        "max_hops": 4,
        "peers": {"bot-b": {"sender_accounts": ["example-user-a"], "aliases": ["bot-b"]}},
    }
    bot, _ = make_bot(tmp_path, collaboration=collab)
    req = "[AGENT]" + encode_agent_message(AgentEnvelope(1, "bot-b", "bot-a", "request", "amsg-r1", None, 0), "帮我检查 patch")
    recognized, routed = bot._route_collaboration_message(incoming(req, sender="example-user-a"))
    assert recognized and routed is not None
    assert routed.content == "帮我检查 patch"
    assert routed.agent_peer_id == "bot-b"

    bad = "[AGENT]" + encode_agent_message(AgentEnvelope(1, "bot-b", "bot-a", "request", "amsg-r2", None, 0), "spoof")
    recognized, routed = bot._route_collaboration_message(incoming(bad, sender="attacker", mid="2"))
    assert recognized and routed is None

    response = "[AGENT]" + encode_agent_message(AgentEnvelope(1, "bot-b", "bot-a", "response", "amsg-r3", "amsg-old", 1), "结果如下")
    recognized, routed = bot._route_collaboration_message(incoming(response, sender="example-user-a", mid="3"))
    assert recognized and routed is None


@pytest.mark.asyncio
async def test_peer_request_reply_is_addressed_back_and_does_not_require_shared_alias(tmp_path: Path):
    collab = {
        "enabled": True,
        "agent_id": "bot-a",
        "groups": ["g"],
        "accept_broadcast": False,
        "max_hops": 4,
        "peers": {"bot-b": {"sender_accounts": ["example-user-a"]}},
    }
    bot, _ = make_bot(tmp_path, collaboration=collab)
    token = bot._collaboration_reply_context.set({
        "conversation_id": "welink:group:g", "peer_id": "bot-b", "message_id": "amsg-request", "hop": 0
    })
    try:
        await bot._send_text("welink:group:g", "这是回复")
    finally:
        bot._collaboration_reply_context.reset(token)
    assert bot.im.sent
    parsed = decode_agent_message(bot.im.sent[-1][1])
    assert parsed is not None
    env, body = parsed
    assert env.from_id == "bot-a"
    assert env.to_id == "bot-b"
    assert env.message_type == "response"
    assert env.reply_to == "amsg-request"
    assert env.hop == 1
    assert body == "这是回复"


@pytest.mark.asyncio
async def test_agent_send_command_emits_targeted_request(tmp_path: Path):
    collab = {
        "enabled": True,
        "agent_id": "bot-a",
        "groups": ["g"],
        "peers": {"bot-b": {"sender_accounts": ["example-user-a"]}},
    }
    bot, _ = make_bot(tmp_path, collaboration=collab)
    msg = incoming("/agent send bot-b 帮我检查测试", sender="example-user-b")
    handled = await bot._handle_lifecycle_command(msg, msg.content)
    assert handled
    parsed = decode_agent_message(bot.im.sent[-1][1])
    assert parsed is not None
    env, body = parsed
    assert env.from_id == "bot-a" and env.to_id == "bot-b" and env.message_type == "request"
    assert body == "帮我检查测试"


def test_collaboration_config_requires_explicit_local_and_peer_sender_identity():
    cfg = {
        "im": {"intent": {"bot_aliases": ["bot-a"]}},
        "collaboration": {
            "enabled": True,
            "agent_id": "bot-a",
            "peers": {"bot-b": {"sender_accounts": ["example-user-a"]}},
        },
    }
    errors = validate_config(cfg)
    assert any("self_accounts" in error for error in errors)

    cfg["im"]["self_accounts"] = ["example-user-b"]
    cfg["im"]["groups"] = [{"group_id": "g"}]
    assert validate_config(cfg) == []


@pytest.mark.asyncio
async def test_peer_request_pending_execution_uses_governed_send_path(tmp_path: Path):
    collab = {
        "enabled": True,
        "agent_id": "bot-a",
        "groups": ["g"],
        "peers": {"bot-b": {"sender_accounts": ["example-user-a"]}},
    }
    bot, _ = make_bot(tmp_path, collaboration=collab)
    pending = PendingAction(
        action_type="agent.peer_request",
        payload={
            "peer_id": "bot-b",
            "instruction": "帮我复核 patch",
            "requested_by": "example-user-b",
            "authorized_by": "example-user-b",
            "authorization_message_id": "m1",
            "origin_message_id": "m1",
        },
        prompt="是否发送？",
    )
    bot.conversations.set_pending_action("welink:group:g", pending)
    await bot._execute_pending(incoming("确认", sender="example-user-b", mid="m2"), pending)

    protocol_messages = []
    for _, text in bot.im.sent:
        parsed = decode_agent_message(text)
        if parsed is not None:
            protocol_messages.append(parsed)
    assert len(protocol_messages) == 1
    env, body = protocol_messages[0]
    assert env.from_id == "bot-a" and env.to_id == "bot-b"
    assert env.message_type == "request"
    assert body == "帮我复核 patch"
    assert bot.conversations.get_pending_action("welink:group:g") is None
