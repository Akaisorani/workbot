from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class NodeSnapshot:
    name: str
    description: str
    online: bool
    active_tasks: int
    max_concurrent: int
    capabilities: tuple[str, ...]
    labels: tuple[str, ...]
    priority: int
    last_change: float | None = None

    @property
    def load_ratio(self) -> float:
        if self.max_concurrent <= 0:
            return 0.0
        return min(1.0, self.active_tasks / self.max_concurrent)


class NodeRegistry:
    """Static node catalog + lightweight runtime state.

    V1.0 intentionally keeps discovery simple: configured SSH nodes are the
    source of truth, while connection state comes from SSHManager and load is
    derived from WorkBot's central task table.  This avoids introducing a new
    broker/heartbeat protocol before it is needed.
    """

    def __init__(self, node_configs: dict[str, dict[str, Any]], store, *, default_node: str | None = None):
        self.configs = {str(k): dict(v or {}) for k, v in (node_configs or {}).items()}
        self.store = store
        self.default_node = default_node
        self._connected: dict[str, bool] = {name: False for name in self.configs}
        self._last_change: dict[str, float] = {}

    def set_connected(self, node: str, connected: bool) -> None:
        if node not in self.configs:
            return
        connected = bool(connected)
        if self._connected.get(node) != connected:
            self._connected[node] = connected
            self._last_change[node] = time.time()

    def is_connected(self, node: str) -> bool:
        return bool(self._connected.get(node, False))

    def _active_count(self, node: str) -> int:
        row = self.store.query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE node=? AND state IN ('created','running','cancelling')",
            (node,),
        )
        return int(row["n"] if row else 0)

    def snapshot(self, node: str) -> NodeSnapshot:
        cfg = self.configs[node]
        caps = tuple(str(x) for x in (cfg.get("capabilities") or []))
        labels = tuple(str(x) for x in (cfg.get("labels") or []))
        return NodeSnapshot(
            name=node,
            description=str(cfg.get("description") or cfg.get("ssh_alias") or node),
            online=self.is_connected(node),
            active_tasks=self._active_count(node),
            max_concurrent=max(1, int(cfg.get("max_concurrent", 4))),
            capabilities=caps,
            labels=labels,
            priority=int(cfg.get("priority", 100)),
            last_change=self._last_change.get(node),
        )

    def all(self) -> list[NodeSnapshot]:
        return [self.snapshot(name) for name in self.configs]

    def planner_configs(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for snap in self.all():
            cfg = dict(self.configs[snap.name])
            cfg["runtime_online"] = snap.online
            cfg["runtime_active_tasks"] = snap.active_tasks
            cfg["runtime_max_concurrent"] = snap.max_concurrent
            cfg["runtime_load_ratio"] = round(snap.load_ratio, 3)
            result[snap.name] = cfg
        return result

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {x.lower() for x in re.findall(r"[A-Za-z0-9_.+-]{2,}|[\u4e00-\u9fff]{2,}", text or "")}

    def choose(self, instruction: str = "", *, preferred: str | None = None,
               required_capabilities: list[str] | tuple[str, ...] | None = None,
               allow_offline: bool = False) -> str | None:
        required = {str(x).lower() for x in (required_capabilities or [])}
        tokens = self._tokens(instruction)
        candidates: list[tuple[float, str]] = []
        for snap in self.all():
            cfg = self.configs[snap.name]
            if cfg.get("enabled", True) is False:
                continue
            if not snap.online and not allow_offline:
                continue
            caps = {x.lower() for x in snap.capabilities}
            labels = {x.lower() for x in snap.labels}
            if required and not required.issubset(caps | labels):
                continue
            score = 0.0
            # Lower numeric priority is preferred, while load/capability match
            # can outweigh small priority differences.
            score -= snap.priority * 0.01
            score -= snap.load_ratio * 4.0
            if snap.active_tasks >= snap.max_concurrent:
                score -= 8.0
            if preferred and snap.name == preferred:
                score += 3.0
            if self.default_node and snap.name == self.default_node:
                score += 1.0
            hints = set(caps) | set(labels) | {str(x).lower() for x in (cfg.get("routing_hints") or [])}
            for hint in hints:
                if hint and (hint in tokens or hint in (instruction or "").lower()):
                    score += 2.0
            candidates.append((score, snap.name))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][1]

    def format_for_agent(self) -> str:
        lines = []
        for s in self.all():
            state = "online" if s.online else "offline"
            caps = ", ".join(s.capabilities) if s.capabilities else "generic-codeagent"
            labels = f"; labels={','.join(s.labels)}" if s.labels else ""
            lines.append(
                f"- {s.name}: {state}; load={s.active_tasks}/{s.max_concurrent}; "
                f"capabilities={caps}{labels}; {s.description}"
            )
        return "\n".join(lines) or "- <no remote nodes configured>"
