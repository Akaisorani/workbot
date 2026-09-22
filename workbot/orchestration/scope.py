from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

LOCAL_NODE = "office-pc"

# Scope resolution is intentionally conservative.  It answers only "which
# execution targets does the user explicitly quantify/reference?"; it does not
# decide how the task should be executed on those targets.
_ALL_NODE_PATTERNS = (
    r"所有(?:已注册)?节点",
    r"全部(?:已注册)?节点",
    r"每个(?:已注册)?节点",
    r"所有机器",
    r"全部机器",
    r"每台机器",
    r"(?i)all\s+nodes?",
    r"(?i)every\s+node",
)
_ALL_REMOTE_PATTERNS = (
    r"所有(?:linux|Linux)节点",
    r"全部(?:linux|Linux)节点",
    r"所有远端节点",
    r"全部远端节点",
    r"所有开发服务器",
    r"全部开发服务器",
    r"所有服务器",
    r"全部服务器",
    r"(?i)all\s+(?:linux|remote)\s+nodes?",
)
_WINDOWS_PATTERNS = (
    r"(?i)(?<![A-Za-z0-9_.-])windows(?![A-Za-z0-9_.-])",
    r"(?i)(?<![A-Za-z0-9_.-])office-pc(?![A-Za-z0-9_.-])",
    r"办公电脑",
    r"工作电脑",
    r"Windows本地",
    r"本机Windows",
)
_CAPABILITY_SCOPE_PATTERNS = (
    ((r"所有x86(?:_64)?节点", r"全部x86(?:_64)?节点", r"所有x86(?:_64)?服务器"), ("x86_64", "x86")),
    ((r"所有(?:arm64|aarch64|ARM)节点", r"全部(?:arm64|aarch64|ARM)节点"), ("aarch64", "arm64", "arm")),
    ((r"所有GPU节点", r"全部GPU节点", r"所有GPU服务器"), ("gpu", "cuda")),
)
_EXECUTION_WORDS = (
    "读取", "查询", "检查", "获取", "查看", "收集", "统计", "运行", "执行", "测试", "验证",
    "编译", "构建", "修改", "修复", "创建", "写入", "删除", "安装", "部署", "更新", "计算", "测量",
    "检测", "扫描", "列出", "打印", "输出", "汇总", "比较", "对比", "分析", "排查", "定位",
)
_FRESH_WORDS = (
    "读取", "查询", "检查", "获取", "查看", "当前", "现在", "实时", "最新", "系统版本", "内核版本",
    "状态", "版本", "负载", "磁盘", "内存", "进程", "git commit", "分支", "uname", "os-release",
)


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    targets: tuple[str, ...] = ()
    source: str = "none"  # none | explicit | all_nodes | all_remote | capability
    requires_execution: bool = False
    fresh_execution: bool = False
    reason: str = ""

    @property
    def multi_target(self) -> bool:
        return len(self.targets) > 1

    @property
    def has_local(self) -> bool:
        return LOCAL_NODE in self.targets

    @property
    def remote_targets(self) -> tuple[str, ...]:
        return tuple(x for x in self.targets if x != LOCAL_NODE)


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _named_remote_nodes(text: str, nodes: dict[str, dict] | Iterable[str]) -> list[str]:
    names = list(nodes.keys()) if isinstance(nodes, dict) else list(nodes)
    found: list[str] = []
    for node in names:
        pat = rf"(?<![A-Za-z0-9_.-]){re.escape(str(node))}(?![A-Za-z0-9_.-])"
        if re.search(pat, text, flags=re.IGNORECASE):
            found.append(str(node))
    return found


def _enabled_nodes(nodes: dict[str, dict]) -> list[str]:
    return [name for name, cfg in nodes.items() if (cfg or {}).get("enabled", True) is not False]


def _linux_nodes(nodes: dict[str, dict]) -> list[str]:
    result: list[str] = []
    for name in _enabled_nodes(nodes):
        cfg = nodes.get(name) or {}
        values = {str(x).lower() for x in (cfg.get("capabilities") or [])}
        values.update(str(x).lower() for x in (cfg.get("labels") or []))
        if "linux" in values:
            result.append(name)
    return result


def _nodes_with_any_capability(nodes: dict[str, dict], aliases: Iterable[str]) -> list[str]:
    wanted = {str(x).lower() for x in aliases}
    result: list[str] = []
    for name in _enabled_nodes(nodes):
        cfg = nodes.get(name) or {}
        values = {str(x).lower() for x in (cfg.get("capabilities") or [])}
        values.update(str(x).lower() for x in (cfg.get("labels") or []))
        values.update(str(x).lower() for x in (cfg.get("routing_hints") or []))
        if values & wanted:
            result.append(name)
    return result


def resolve_execution_scope(text: str, nodes: dict[str, dict]) -> ExecutionScope:
    """Resolve deterministic execution targets from user wording.

    The resolver deliberately does *not* infer a task taxonomy or invent
    targets.  It only expands explicit quantifiers/references against the Node
    Registry so orchestration can enforce coverage independently of the LLM.
    """
    value = (text or "").strip()
    low = value.lower()
    requires_execution = any(word.lower() in low for word in _EXECUTION_WORDS)
    fresh = any(word.lower() in low for word in _FRESH_WORDS)

    if _matches_any(value, _ALL_NODE_PATTERNS):
        targets = [LOCAL_NODE, *_enabled_nodes(nodes)]
        return ExecutionScope(
            tuple(dict.fromkeys(targets)), "all_nodes", requires_execution, fresh,
            "用户明确要求所有节点；由框架展开为 office-pc + 所有 enabled 远端节点。",
        )

    if _matches_any(value, _ALL_REMOTE_PATTERNS):
        targets = _linux_nodes(nodes)
        return ExecutionScope(
            tuple(dict.fromkeys(targets)), "all_remote", requires_execution, fresh,
            "用户明确要求所有 Linux/远端节点；由 Node Registry capability 展开。",
        )

    for patterns, aliases in _CAPABILITY_SCOPE_PATTERNS:
        if _matches_any(value, patterns):
            targets = _nodes_with_any_capability(nodes, aliases)
            return ExecutionScope(
                tuple(dict.fromkeys(targets)), "capability", requires_execution, fresh,
                f"用户按节点能力范围指定目标；匹配 capabilities/labels/routing_hints: {', '.join(aliases)}。",
            )

    explicit = _named_remote_nodes(value, nodes)
    if _matches_any(value, _WINDOWS_PATTERNS) or re.search(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]", value):
        explicit.insert(0, LOCAL_NODE)
    explicit = list(dict.fromkeys(explicit))
    if explicit:
        return ExecutionScope(
            tuple(explicit), "explicit", requires_execution, fresh,
            "用户在消息中显式指定了执行节点。",
        )

    return ExecutionScope((), "none", requires_execution, fresh, "未发现确定性的节点范围表达。")
