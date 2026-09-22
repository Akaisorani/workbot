from __future__ import annotations

import json
from pathlib import Path

import pytest

from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager, _looks_like_meta_only_delivery
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.im.welink import WeLinkAdapter, WeLinkSendAmbiguousError
from workbot.main import WorkBot
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store


class SequenceBackend:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def run(self, prompt, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        result = self.results.pop(0)
        sid = kwargs.get("session_id") or kwargs.get("new_session_id")
        return AgentResult(result, session_id=sid)


def make_manager(tmp_path: Path, results) -> tuple[AgentManager, SequenceBackend]:
    store = Store(tmp_path / "agent.sqlite")
    conversations = ConversationManager(store)
    conversations.add_incoming(IncomingMessage(
        "welink", "welink:group:g", "group", "g", "m1", "u1",
        'bot 总结"Example Engineering Group"群最近的消息然后告诉我',
        1, "User", transport="welinkbot",
    ))
    manager = AgentManager(
        {"command": "codeagent", "max_concurrent": 1}, tmp_path,
        conversations, MemoryManager(store, {}),
    )
    backend = SequenceBackend(results)
    manager.backend = backend
    return manager, backend


def test_meta_only_delivery_detector_is_conservative():
    assert _looks_like_meta_only_delivery("Already processed and summary delivered to the user.")
    assert _looks_like_meta_only_delivery("Summary delivered to the user")
    assert _looks_like_meta_only_delivery("已总结并发送给你。")
    assert not _looks_like_meta_only_delivery("总结如下：今天主要讨论了三个问题……")
    assert not _looks_like_meta_only_delivery("消息已经发送失败，需要重试。具体错误是 timeout。")


@pytest.mark.asyncio
async def test_meta_only_agent_reply_recovers_existing_result_without_tools(tmp_path: Path):
    summary = "群聊总结：\n- Stream PBE 条件下推是主要讨论点。\n- 下一步确认 Result 算子是否可移除。"
    manager, backend = make_manager(tmp_path, [
        "Already processed and summary delivered to the user.",
        summary,
    ])

    result = await manager.answer(
        "welink:group:g",
        '总结"Example Engineering Group"群最近的消息然后告诉我',
    )

    assert result.text == summary
    assert len(backend.calls) == 2
    assert backend.calls[0][1].get("tool_mode") == "execute"
    assert backend.calls[1][1].get("tool_mode") == "disabled"
    assert "Do NOT call any tools" in backend.calls[1][0]
    assert "reproduce the full substantive" in backend.calls[1][0]


@pytest.mark.asyncio
async def test_normal_agent_reply_is_not_reinvoked(tmp_path: Path):
    manager, backend = make_manager(tmp_path, ["群聊总结：今天讨论了 Stream PBE。"])
    result = await manager.answer("welink:group:g", "总结群聊然后告诉我")
    assert result.text.startswith("群聊总结")
    assert len(backend.calls) == 1


class FailThenCaptureWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__("welink-cli", [], bootstrap_from_latest=False)
        self.calls = []
        self.failed = False

    async def send_text(self, conversation_id: str, text: str, *, created_at_ms=None, verify_before_send=False):
        self.calls.append((conversation_id, text, verify_before_send))
        if not self.failed:
            self.failed = True
            raise WeLinkSendAmbiguousError("verification timeout")
        return None


def make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "access_control": {"default": "allow"},
        "im": {"type": "welink", "groups": []},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "nodes": {},
        "outbound": {"retry_seconds": 1, "retry_max_seconds": 2},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    return WorkBot(cp)


@pytest.mark.asyncio
async def test_send_failure_retries_exact_persisted_reply_without_agent(tmp_path: Path):
    bot = make_bot(tmp_path)
    adapter = FailThenCaptureWeLink()
    bot.im = adapter
    original = "群聊总结正文：\n1. 讨论 A\n2. 讨论 B\n3. 讨论 C"

    await bot._send_text("welink:group:g", original)
    row = bot.store.query_one("SELECT * FROM outbound_messages ORDER BY created_at_ms LIMIT 1")
    assert row["state"] == "pending"
    assert row["text"] == original

    # Make the durable row immediately eligible and retry that row directly.
    bot.store.execute("UPDATE outbound_messages SET next_attempt_at=0 WHERE send_id=?", (row["send_id"],))
    delivered = await bot._attempt_outbound(
        row["send_id"], row["conversation_id"], row["text"], row["created_at_ms"],
        verify_before_send=True,
    )
    assert delivered is True
    assert len(adapter.calls) == 2
    assert adapter.calls[0][1] == original
    assert adapter.calls[1][1] == original
    assert adapter.calls[1][2] is True
    done = bot.store.query_one("SELECT state,text FROM outbound_messages WHERE send_id=?", (row["send_id"],))
    assert done["state"] == "delivered"
    assert done["text"] == original
