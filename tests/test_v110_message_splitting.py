import json
from pathlib import Path

import pytest

from workbot.im.welink import WeLinkAdapter, WeLinkSendAmbiguousError
from workbot.im.welink_format import WELINK_CODE_START, count_code_blocks, render_welink_markdown, split_welink_markdown
from workbot.main import WorkBot


def make_bot(tmp_path: Path, *, max_chars: int = 700) -> WorkBot:
    (tmp_path / 'config').mkdir()
    cfg = {
        'database': 'state/workbot.db',
        'access_control': {'default': 'allow'},
        'im': {'type': 'welink', 'groups': [{'group_id': 'g1', 'group_name': 'g1'}]},
        'agent': {'command': 'codeagent'},
        'nodes': {},
        'outbound': {'retry_seconds': 2, 'retry_max_seconds': 4, 'welink_max_message_chars': max_chars},
    }
    p = tmp_path / 'config' / 'local.json'
    p.write_text(json.dumps(cfg), encoding='utf-8')
    return WorkBot(p)


class CaptureWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__('welink-cli', [{'group_id': 'g1', 'group_name': 'g1'}], bootstrap_from_latest=False)
        self.sent = []

    def _send_once(self, conv, text):
        self.sent.append(text)


class FailFirstWeLink(CaptureWeLink):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send_text(self, conversation_id: str, text: str, *, created_at_ms=None, verify_before_send=False):
        self.calls += 1
        if self.calls == 1:
            raise WeLinkSendAmbiguousError('verification timeout')
        return await super().send_text(
            conversation_id, text, created_at_ms=created_at_ms,
            verify_before_send=verify_before_send,
        )


def test_two_code_blocks_are_split_and_source_order_is_preserved():
    source = (
        'hello world：\n```cpp\n#include <stdio.h>\nint main(){return 0;}\n```\n'
        '编译指令：\n```bash\ngcc hello.c -o hello\n./hello\n```\n完成。'
    )
    parts = split_welink_markdown(source, max_chars=700)
    assert len(parts) == 2
    assert all(count_code_blocks(part) <= 1 for part in parts)
    assert 'int main' in parts[0]
    assert 'gcc hello.c' in parts[1]


def test_long_plain_text_is_split_without_loss():
    source = ('这是一个用于验证长消息拆分的中文段落。\n' * 300).strip()
    parts = split_welink_markdown(source, max_chars=700)
    assert len(parts) > 1
    assert all(len(render_welink_markdown(part)) <= 700 for part in parts)
    # Splitter may trim only delimiter newlines at message boundaries.  Compare
    # semantic lines rather than raw boundary whitespace.
    original_lines = [x for x in source.splitlines() if x]
    split_lines = [x for part in parts for x in part.splitlines() if x]
    assert split_lines == original_lines


def test_single_oversized_code_block_is_rewrapped_as_multiple_complete_blocks():
    source = '```cpp\n' + ''.join(f'int value_{i} = {i};\n' for i in range(300)) + '```'
    parts = split_welink_markdown(source, max_chars=700)
    assert len(parts) > 1
    assert all(part.startswith('```cpp\n') and part.endswith('```') for part in parts)
    assert all(count_code_blocks(part) == 1 for part in parts)
    assert all(len(render_welink_markdown(part)) <= 700 for part in parts)
    joined = '\n'.join(part.split('\n', 1)[1].rsplit('\n```', 1)[0] for part in parts)
    assert 'int value_0 = 0;' in joined
    assert 'int value_299 = 299;' in joined


def test_stream_pbe_regression_keeps_all_sections_and_one_block_per_message():
    source = '''关键字段说明：
- `num_params`：参数个数
- `param_values`：参数二进制值

### 完整交互时序
```text
客户端          CN              DN1
  │ EXECUTE p(100) │             │
  │───────────────>│             │
```

### 核心优化点
| 对比项 | 传统 Stream 执行 | Stream PBE |
|---|---|---|
| 后续 EXECUTE | 重复序列化 | 复用缓存 |

### 前置 GUC
```sql
SET enable_stream_pbe = on;
SET enable_stream_operator = on;
```

源码位置：stream_pbe_sender.cpp
'''
    parts = split_welink_markdown(source, max_chars=700)
    assert len(parts) >= 3  # diagram + table + SQL become distinct native blocks
    assert all(count_code_blocks(part) <= 1 for part in parts)
    combined = '\n'.join(parts)
    for anchor in ('num_params', '完整交互时序', '核心优化点', '前置 GUC', 'stream_pbe_sender.cpp'):
        assert anchor in combined


@pytest.mark.asyncio
async def test_workbot_sends_every_part_with_prefix_and_durable_rows(tmp_path: Path):
    bot = make_bot(tmp_path, max_chars=700)
    adapter = CaptureWeLink()
    bot.im = adapter
    source = '说明\n```cpp\nint main(){}\n```\n编译\n```bash\ng++ a.cpp -o a\n./a\n```\n' + ('补充说明。' * 250)
    await bot._send_text('welink:group:g1', source)

    rows = bot.store.query_all('SELECT * FROM outbound_messages ORDER BY created_at_ms')
    assert len(rows) == len(adapter.sent) >= 2
    assert all(row['state'] == 'delivered' for row in rows)
    assert all(adapter_payload.startswith('[AGENT]') for adapter_payload in adapter.sent)
    assert all(len(adapter_payload) <= 700 for adapter_payload in adapter.sent)
    assert all(adapter_payload.count(WELINK_CODE_START) <= 1 for adapter_payload in adapter.sent)
    assert [int(row['created_at_ms']) for row in rows] == sorted(int(row['created_at_ms']) for row in rows)


@pytest.mark.asyncio
async def test_failed_first_part_does_not_send_later_parts_out_of_order(tmp_path: Path):
    bot = make_bot(tmp_path, max_chars=700)
    adapter = FailFirstWeLink()
    bot.im = adapter
    source = 'A\n```cpp\nint a;\n```\nB\n```python\nprint(2)\n```\nC'
    await bot._send_text('welink:group:g1', source)

    rows = bot.store.query_all('SELECT * FROM outbound_messages ORDER BY created_at_ms')
    assert len(rows) == 2
    assert adapter.calls == 1  # second part was queued, not sent ahead of part 1
    assert rows[0]['state'] == 'pending'
    assert rows[1]['state'] == 'pending'
    assert int(rows[0]['attempts']) == 1
    assert int(rows[1]['attempts']) == 0
