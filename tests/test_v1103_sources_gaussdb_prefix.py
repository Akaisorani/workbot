from __future__ import annotations

from pathlib import Path

import pytest

from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.im.welink import WeLinkAdapter
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store


class CaptureBackend:
    def __init__(self, text: str = "ok"):
        self.text = text
        self.calls: list[tuple[str, dict]] = []

    async def run(self, prompt: str, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        sid = kwargs.get("session_id") or kwargs.get("new_session_id")
        return AgentResult(self.text, session_id=sid)


def make_manager(tmp_path: Path, cfg: dict | None = None) -> AgentManager:
    store = Store(tmp_path / "v1103.sqlite")
    conv = ConversationManager(store)
    conv.add_incoming(IncomingMessage(
        "welink", "welink:group:g", "group", "g", "m1", "u1",
        "bot stream operator rescan 是怎么执行的？", 1, "User", transport="welinkbot",
    ))
    return AgentManager(cfg or {"command": "codeagent", "max_concurrent": 1}, tmp_path, conv, MemoryManager(store, {}))


def test_direct_prompt_requires_inline_source_attribution_and_gaussdb_defaults(tmp_path: Path):
    manager = make_manager(tmp_path)
    prompt = manager._context_prompt("welink:group:g", "result 算子和 stream rescan 的执行逻辑是什么？")

    assert "attach a compact source marker immediately after" in prompt
    assert "[源码: relative/or/absolute/file.cpp::Symbol]" in prompt
    assert "[产品手册: 《document title》 -> chapter/section]" in prompt
    assert "[Wiki: page title | URL]" in prompt
    assert "[W3/Web: page title | URL]" in prompt
    assert "<not configured>" in prompt
    assert "wiki-mcp" in prompt
    assert "wiki.example.com/workbot" not in prompt
    assert "treat GaussDB as the likely background" in prompt
    assert "current implementation -> code" in prompt


def test_gaussdb_context_is_configurable_without_code_change(tmp_path: Path):
    manager = make_manager(tmp_path, {
        "command": "codeagent",
        "gaussdb_context": {
            "source_root": r"C:\example\source",
            "manual_root": r"C:\example\manuals",
            "wiki_entry_url": "https://wiki.example/entry",
        },
    })
    prompt = manager._gaussdb_context_prompt()
    assert r"C:\example\source" in prompt
    assert r"C:\example\manuals" in prompt
    assert "https://wiki.example/entry" in prompt

    disabled = make_manager(tmp_path / "disabled", {
        "command": "codeagent",
        "gaussdb_context": {"enabled": False},
    })
    assert disabled._gaussdb_context_prompt() == ""
    assert "Evidence/source attribution" in disabled._answer_grounding_prompt()


@pytest.mark.asyncio
async def test_readonly_reply_prompt_includes_same_grounding_rules(tmp_path: Path):
    manager = make_manager(tmp_path)
    backend = CaptureBackend(
        '<WORKBOT_REPLY>{"can_reply":true,"confidence":0.95,"answer":"结论 [源码: src/a.cpp::f]","reason":"grounded"}</WORKBOT_REPLY>'
    )
    manager.backend = backend
    result = await manager.answer_read_only("welink:group:g", "这个执行逻辑是什么？")
    assert result["can_reply"] is True
    assert result["answer"].endswith("[源码: src/a.cpp::f]")
    assert len(backend.calls) == 1
    prompt = backend.calls[0][0]
    assert "Evidence/source attribution" in prompt
    assert "<not configured>" in prompt
    assert "wiki-mcp" in prompt
    assert backend.calls[0][1].get("tool_mode") == "readonly"


def test_default_reply_prefix_is_agent_and_legacy_prefix_is_still_suppressed():
    adapter = WeLinkAdapter("welink-cli", [], bootstrap_from_latest=False)
    assert adapter.reply_prefix == "[AGENT]"
    assert adapter._is_bot_reply("group:g", "[AGENT]当前回复") is True
    assert adapter._is_bot_reply("group:g", "[自动回复]升级前的历史回复") is True
    assert adapter._is_bot_reply("group:g", "普通用户消息") is False
