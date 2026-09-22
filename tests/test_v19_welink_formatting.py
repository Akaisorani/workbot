import re
import unicodedata

import pytest

from workbot.im.welink import WeLinkAdapter
from workbot.im.welink_format import (
    WELINK_CODE_END,
    WELINK_CODE_START,
    count_code_blocks,
    encode_welink_code_block,
    normalize_markdown_for_welink,
    render_welink_markdown,
)


def _width(text: str) -> int:
    value = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        value += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return value


def test_native_code_block_matches_observed_welink_shape():
    value = encode_welink_code_block('import os\n\nprint("python code")\n', 'python')
    assert value.startswith(WELINK_CODE_START + 'import os\n\nprint("python code")\n')
    assert '"lang":"python"' in value
    assert '"lineBreak":false' in value
    assert '"totalLines":3' in value
    assert value.endswith(WELINK_CODE_END)


def test_language_aliases_and_unsupported_fallback_are_supported_by_welink():
    bash = encode_welink_code_block('gcc hello.c -o hello', 'bash')
    json_block = encode_welink_code_block('{"ok":true}', 'json')
    assert '"lang":"python"' in bash
    assert '"lang":"javascript"' in json_block


def test_markdown_table_becomes_monospaced_box_with_cjk_width_alignment():
    source = '| 项目 | Value |\n|---|---|\n| 中文 | abc |\n| long | 12345 |'
    normalized = normalize_markdown_for_welink(source)
    assert normalized.startswith('```workbot-table\n')
    rendered = render_welink_markdown(source)
    assert rendered.count(WELINK_CODE_START) == 1
    assert '┌' in rendered and '┼' in rendered and '┘' in rendered

    # Extract the table body before metadata and verify all box rows have the
    # same terminal display width even with CJK cells.
    body = rendered.split(WELINK_CODE_START, 1)[1].split('\n{"lang":', 1)[0]
    rows = [line for line in body.splitlines() if line]
    assert len({_width(line) for line in rows}) == 1


def test_surrounding_text_is_preserved_around_native_code_block():
    source = '下面是一段示例代码块\n```python\nimport os\n\nprint("python code")\n```\n上面是一段示例代码块'
    rendered = render_welink_markdown(source)
    assert rendered.startswith('下面是一段示例代码块\n' + WELINK_CODE_START)
    assert rendered.rstrip().endswith('上面是一段示例代码块')
    assert rendered.count(WELINK_CODE_START) == 1


class CaptureWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__('welink-cli', [{'group_id': 'g1', 'group_name': 'g1'}], bootstrap_from_latest=False)
        self.sent = []

    def _send_once(self, conv, text):
        self.sent.append((conv.key, text))


@pytest.mark.asyncio
async def test_adapter_renders_markdown_before_cli_send():
    adapter = CaptureWeLink()
    await adapter.send_text('welink:group:g1', '代码：\n```cpp\nint main() {}\n```')
    assert len(adapter.sent) == 1
    _, payload = adapter.sent[0]
    assert payload.startswith('[AGENT]代码：\n')
    assert payload.count(WELINK_CODE_START) == 1
    assert '"lang":"cpp"' in payload
    assert '```' not in payload
