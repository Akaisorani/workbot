from __future__ import annotations

import re
from typing import Iterable

_REMOTE_ACTION_WORDS = (
    "任务", "让codeagent", "让 codeagent", "执行", "运行", "创建", "修改", "修复",
    "实现", "开发", "编译", "构建", "测试", "验证", "查看", "检查", "排查", "定位",
    "分析", "写入", "删除", "安装", "部署", "更新", "生成", "运行一下", "跑一下",
)
_LOCAL_HINTS = (
    "windows", "本地电脑", "工作电脑", "办公电脑", "本机", "本地文件", "电脑文件",
    "本地资料", "本地目录", "pc", "workbot workspace",
)


def configured_nodes_in_text(text: str, nodes: Iterable[str]) -> list[str]:
    found: list[str] = []
    for node in nodes:
        pat = rf"(?<![A-Za-z0-9_.-]){re.escape(node)}(?![A-Za-z0-9_.-])"
        if re.search(pat, text, flags=re.IGNORECASE):
            found.append(node)
    return found


def configured_node_in_text(text: str, nodes: Iterable[str]) -> str | None:
    values = configured_nodes_in_text(text, nodes)
    return values[0] if values else None


def is_explicit_remote_action(text: str, nodes: Iterable[str]) -> bool:
    low = text.lower()
    return bool(configured_nodes_in_text(text, nodes)) and any(word in low for word in _REMOTE_ACTION_WORDS)


def has_windows_local_hint(text: str) -> bool:
    low = text.lower()
    if any(hint in low for hint in _LOCAL_HINTS):
        return True
    # A Windows drive path is an unambiguous local-PC hint for this WorkBot.
    return bool(re.search(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]", text))


def looks_like_mixed_workflow(text: str, nodes: Iterable[str]) -> bool:
    """Conservative trigger for local+remote orchestration.

    We intentionally do not classify domain task types.  A workflow is inferred
    only when the message clearly references a configured remote node and also
    references Windows/local-PC material, or explicitly asks to combine results.
    Users can always force planning with ``/workflow``.
    """
    if not configured_nodes_in_text(text, nodes):
        return False
    if has_windows_local_hint(text):
        return True
    low = text.lower()
    combine_hints = ("结合起来", "结合结果", "汇总", "综合", "分别", "两边", "不同节点", "然后结合")
    return any(x in low for x in combine_hints)


def preview(text: str, max_chars: int = 260) -> str:
    value = " ".join(text.strip().split())
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"
