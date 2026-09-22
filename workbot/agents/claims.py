from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# These are WorkBot-managed state transitions.  They are intentionally narrower
# than generic phrases such as "编译完成": CodeAgent may legitimately execute a
# local build itself, while creation/running state for remote tasks/workflows is
# authoritative only when WorkBot has a durable receipt.
_REMOTE_TASK_PATTERNS = (
    re.compile(r"(?:已|已经|现已|成功)?\s*(?:发起|创建|提交|启动|下发|派发|分发)\s*(?:了)?\s*(?:远程|远端)\s*(?:CodeAgent\s*)?任务", re.I),
    re.compile(r"(?:远程|远端)\s*(?:CodeAgent\s*)?任务\s*(?:已|已经|现已|正在)\s*(?:创建|启动|运行|执行)", re.I),
    re.compile(r"(?:已|已经|现已)\s*创建\s*(?:了)?\s*(?:远程|远端)?\s*任务\s*task-[A-Za-z0-9_-]+", re.I),
)
_WORKFLOW_PATTERNS = (
    re.compile(r"(?:已|已经|现已|成功)?\s*(?:发起|创建|提交|启动)\s*(?:了)?\s*(?:多节点)?\s*工作流", re.I),
    re.compile(r"(?:工作流|workflow)\s*(?:已|已经|现已|正在)\s*(?:创建|启动|运行|执行)", re.I),
)
_TASK_ID_RE = re.compile(r"\btask-[A-Za-z0-9_-]+\b", re.I)
_WORKFLOW_ID_RE = re.compile(r"\bwf-[A-Za-z0-9_-]+\b", re.I)
_FENCE_RE = re.compile(r"```.*?```", re.S)
_NEGATION_HINTS = (
    "未发起", "没有发起", "尚未发起", "并未发起", "未创建", "没有创建", "尚未创建", "并未创建",
    "未启动", "没有启动", "尚未启动", "并未启动", "未执行", "没有执行", "尚未执行", "并未执行",
    "无法发起", "不能发起", "不会发起", "虚假", "幻觉", "错误声明", "不代表", "并不代表", "不是已",
    "not created", "not started", "not launched", "not submitted", "has not been", "was not",
)


@dataclass(slots=True, frozen=True)
class ManagedStateClaim:
    kind: str
    fragment: str
    task_ids: tuple[str, ...] = ()
    workflow_ids: tuple[str, ...] = ()


def _plain_sentences(text: str) -> list[str]:
    value = _FENCE_RE.sub("", str(text or ""))
    # Ignore Markdown blockquotes because they are often quoted logs/user text.
    lines = [line for line in value.splitlines() if not line.lstrip().startswith(">")]
    value = "\n".join(lines)
    return [x.strip() for x in re.split(r"(?<=[。！？!?])|\n+", value) if x.strip()]


def _negated(sentence: str) -> bool:
    low = sentence.lower()
    return any(h.lower() in low for h in _NEGATION_HINTS)


def detect_managed_state_claim(text: str, managed_nodes: Iterable[str] = ()) -> ManagedStateClaim | None:
    """Detect an affirmative claim about WorkBot-managed remote state.

    This deliberately does not flag generic local execution claims.  The guard
    protects state that WorkBot itself owns and can verify with durable task /
    workflow receipts.
    """
    nodes = [str(x).strip() for x in managed_nodes if str(x).strip()]
    node_patterns = [
        re.compile(
            rf"(?:已|已经|现已)?\s*(?:在|到|至)?\s*{re.escape(node)}\s*(?:上)?\s*(?:正在|已开始|已经开始)\s*(?:执行|运行|编译|处理)",
            re.I,
        )
        for node in nodes
    ]
    for sentence in _plain_sentences(text):
        if _negated(sentence):
            continue
        kind = ""
        if any(p.search(sentence) for p in _REMOTE_TASK_PATTERNS) or any(p.search(sentence) for p in node_patterns):
            kind = "remote_task"
        elif any(p.search(sentence) for p in _WORKFLOW_PATTERNS):
            kind = "workflow"
        if not kind:
            continue
        return ManagedStateClaim(
            kind=kind,
            fragment=sentence[:500],
            task_ids=tuple(_TASK_ID_RE.findall(sentence)),
            workflow_ids=tuple(_WORKFLOW_ID_RE.findall(sentence)),
        )
    return None


def strip_managed_state_claims(text: str, managed_nodes: Iterable[str] = ()) -> str:
    """Remove affirmative WorkBot-managed state sentences from an Agent preamble.

    Used when the same turn contains a structured WORKBOT_ACTION.  The action is
    only a proposal; any prose saying it has already been launched is therefore
    non-authoritative and must not be shown before WorkBot creates a receipt.
    """
    kept: list[str] = []
    for paragraph in re.split(r"(\n+)", str(text or "")):
        if not paragraph or paragraph.startswith("\n"):
            kept.append(paragraph)
            continue
        sentences = re.split(r"(?<=[。！？!?])", paragraph)
        filtered = [s for s in sentences if not detect_managed_state_claim(s, managed_nodes)]
        kept.append("".join(filtered))
    value = "".join(kept)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
