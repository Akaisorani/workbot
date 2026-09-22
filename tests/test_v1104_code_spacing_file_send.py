from __future__ import annotations

from pathlib import Path

import pytest

from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager
from workbot.conversation.manager import ConversationManager
from workbot.im.welink_format import WELINK_CODE_END, render_welink_markdown
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store
from workbot.tools.welink_capabilities import capability_prompt, classify_welink_argv


class CaptureBackend:
    def __init__(self, text: str = "ok"):
        self.text = text
        self.calls: list[tuple[str, dict]] = []

    async def run(self, prompt: str, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        sid = kwargs.get("session_id") or kwargs.get("new_session_id")
        return AgentResult(self.text, session_id=sid)


def make_manager(tmp_path: Path) -> AgentManager:
    store = Store(tmp_path / "v1104.sqlite")
    conv = ConversationManager(store)
    return AgentManager({"command": "codeagent", "max_concurrent": 1}, tmp_path, conv, MemoryManager(store, {}))


def _after_native_code(rendered: str) -> str:
    pos = rendered.rfind(WELINK_CODE_END)
    assert pos >= 0
    return rendered[pos + len(WELINK_CODE_END):]


def test_code_block_render_does_not_add_extra_blank_line():
    source = '```python\nD:\\work\\file.docx\n```\n正文'
    rendered = render_welink_markdown(source)
    assert _after_native_code(rendered) == '\n正文'


def test_code_block_render_compacts_model_blank_lines_after_fence():
    source = '```python\nD:\\work\\file.docx\n```\n\n\n\n正文'
    rendered = render_welink_markdown(source)
    assert _after_native_code(rendered) == '\n正文'


def test_code_block_render_compacts_whitespace_only_blank_lines():
    source = '```python\nx\n```\n  \n\t\n正文'
    rendered = render_welink_markdown(source)
    assert _after_native_code(rendered) == '\n正文'


def test_plain_text_spacing_is_not_globally_collapsed():
    source = '第一段\n\n\n第二段'
    assert render_welink_markdown(source) == source


def test_direct_file_send_is_classified_as_one_im_send_write():
    group = classify_welink_argv([
        'im', 'send-to-group', '--group-id', '1001', '--file', r'D:\\work\\a.docx'
    ])
    user = classify_welink_argv([
        'im', 'send-to-user', '--receiver', 'peer-account', '--file', r'D:\\work\\a.docx'
    ])
    assert group.mode == 'write' and group.action == 'im.send'
    assert user.mode == 'write' and user.action == 'im.send'
    text = capability_prompt()
    assert 'Direct IM file attachments ARE supported' in text
    assert 'do NOT separately perform OneBox' in text


def test_group_conversation_prompt_exposes_exact_attachment_target(tmp_path: Path):
    manager = make_manager(tmp_path)
    prompt = manager._context_prompt(
        'welink:group:1104',
        r'把 D:\\workbot\\docs\\sample-attachment.docx 发给我',
    )
    assert 'Direct FILE ATTACHMENTS are an explicit exception' in prompt
    assert 'request exactly one `im.send` approval' in prompt
    assert 'welink-cli im send-to-group --group-id "1104" --file' in prompt
    assert 'Do not pre-upload the file to OneBox' in prompt
    assert 'Do not claim that `welink-cli` only supports text' in prompt


def test_private_conversation_prompt_exposes_exact_attachment_target(tmp_path: Path):
    manager = make_manager(tmp_path)
    prompt = manager._context_prompt('welink:user:peer-account', '把文件发给我')
    assert 'welink-cli im send-to-user --receiver "peer-account" --file' in prompt


@pytest.mark.asyncio
async def test_im_send_approval_resume_repeats_current_attachment_target(tmp_path: Path):
    manager = make_manager(tmp_path)
    backend = CaptureBackend('发送完成')
    manager.backend = backend
    result = await manager.continue_after_approval(
        'welink:group:1104',
        action='im.send',
        summary='发送设计文档附件',
        approval_token='token-file',
    )
    assert result.text == '发送完成'
    assert len(backend.calls) == 1
    prompt, kwargs = backend.calls[0]
    assert 'welink-cli im send-to-group --group-id "1104" --file' in prompt
    assert kwargs.get('tool_mode') == 'execute'
    assert kwargs.get('approval_token') == 'token-file'
