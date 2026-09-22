from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

_STANDARD_RELAY = "python3 -u ~/.workbot/worker_main.py relay"

# Only defaults that are already hard-coded in the runtime are eligible for
# pruning.  Unknown/new keys are intentionally preserved.
_SAFE_DEFAULTS: dict[tuple[str, ...], Any] = {
    ("database",): "state/workbot.db",
    ("poll_interval_seconds",): 3,
    ("agent_failure_cooldown_seconds",): 60,
    ("workflow_step_timeout_seconds",): 3600,
    ("conversation", "summary_every_messages"): 12,
    ("conversation", "summary_min_messages"): 8,
    ("memory", "auto_capture"): True,
    ("memory", "allow_summary_auto_capture"): False,
    ("memory", "auto_global"): False,
    ("memory", "conversation_batch_messages"): 60,
    ("memory", "promotion_min_candidates"): 4,
    ("memory", "promotion_night_start_hour"): 1,
    ("memory", "promotion_night_end_hour"): 5,
    ("maintenance", "interval_seconds"): 120,
    ("welink_tools", "managed_gateway"): True,
    ("welink_tools", "gateway_trace"): False,
    ("welink_tools", "shared_gate_path"): "state/welink-cli.gate",
    ("welink_tools", "shared_rate_path"): "state/welink-rate.json",
    ("workspace_knowledge", "roots"): [],
    ("workspace_knowledge", "idle_grace_seconds"): 300,
    ("workspace_knowledge", "night_start_hour"): 1,
    ("workspace_knowledge", "night_end_hour"): 6,
    ("workspace_knowledge", "project_discovery_depth"): 3,
    ("workspace_knowledge", "index_path_depth"): 12,
    ("workspace_knowledge", "max_projects_per_root"): 100,
    ("workspace_knowledge", "discovery_interval_seconds"): 3600,
    ("workspace_knowledge", "scan_interval_seconds"): 21600,
    ("workspace_knowledge", "night_scan_interval_seconds"): 3600,
    ("workspace_knowledge", "allow_daytime_idle"): True,
    ("workspace_knowledge", "max_files_per_project"): 4000,
    ("workspace_knowledge", "max_file_bytes"): 262144,
    ("workspace_knowledge", "chunk_chars"): 6000,
    ("workspace_knowledge", "max_chunks_per_file"): 24,
    ("workspace_knowledge", "analysis_max_files"): 36,
    ("workspace_knowledge", "analysis_file_chars"): 5000,
    ("workspace_knowledge", "analysis_max_chars"): 100000,
    ("rpc", "max_response_chars"): 16000,
    ("rpc", "max_file_chars"): 64000,
    ("rpc", "timeout_seconds"): 300,
    ("rpc", "max_concurrent"): 2,
}

_REMOTE_DEFAULTS = {
    "project_discovery_depth": 2,
    "index_path_depth": 5,
    "max_projects": 20,
    "max_files_per_project": 1200,
    "max_file_bytes": 131072,
    "chunk_chars": 4000,
    "max_chunks_per_project": 160,
    "max_index_chars_per_project": 600000,
    "discovery_interval_seconds": 3600,
    "scan_interval_seconds": 21600,
    "night_scan_interval_seconds": 3600,
    "index_root_without_marker": False,
}


def _drop_path(obj: dict, path: tuple[str, ...], default: Any) -> int:
    cur: Any = obj
    for key in path[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return 0
        cur = cur[key]
    key = path[-1]
    if isinstance(cur, dict) and key in cur and cur[key] == default:
        del cur[key]
        return 1
    return 0


def _remove_empty_known_sections(obj: dict) -> None:
    # Do not recursively delete arbitrary empty user objects; only known sections
    # where absence has exactly the same runtime meaning.
    for key in ("conversation", "maintenance", "welink_tools"):
        if obj.get(key) == {}:
            obj.pop(key, None)
    wk = obj.get("workspace_knowledge")
    if isinstance(wk, dict) and wk.get("remote_defaults") == {}:
        wk.pop("remote_defaults", None)
    rpc = obj.get("rpc")
    if isinstance(rpc, dict) and rpc == {}:
        obj.pop("rpc", None)


def compact_config(config: dict) -> tuple[dict, int]:
    out = deepcopy(config)
    removed = 0
    for path, default in _SAFE_DEFAULTS.items():
        removed += _drop_path(out, path, default)

    wk = out.get("workspace_knowledge")
    if isinstance(wk, dict) and isinstance(wk.get("remote_defaults"), dict):
        rd = wk["remote_defaults"]
        for key, default in _REMOTE_DEFAULTS.items():
            if key in rd and rd[key] == default:
                del rd[key]
                removed += 1
        if not rd:
            wk.pop("remote_defaults", None)
            removed += 1

    nodes = out.get("nodes")
    if isinstance(nodes, dict):
        for _name, node in nodes.items():
            if not isinstance(node, dict):
                continue
            for key, default in (
                ("relay_command", _STANDARD_RELAY),
                ("max_concurrent", 4),
                ("priority", 100),
                ("enabled", True),
                ("labels", []),
                ("routing_hints", []),
            ):
                if key in node and node[key] == default:
                    del node[key]
                    removed += 1
            nwk = node.get("workspace_knowledge")
            if isinstance(nwk, dict):
                for key, default in _REMOTE_DEFAULTS.items():
                    if key in nwk and nwk[key] == default:
                        del nwk[key]
                        removed += 1
                if not nwk:
                    node.pop("workspace_knowledge", None)
                    removed += 1

    _remove_empty_known_sections(out)
    return out, removed


def _render(value: Any, level: int = 0, *, indent: int = 2) -> str:
    pad = " " * (level * indent)
    child_pad = " " * ((level + 1) * indent)
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        for key, val in value.items():
            rendered = _render(val, level + 1, indent=indent)
            parts.append(f"{child_pad}{json.dumps(str(key), ensure_ascii=False)}: {rendered}")
        return "{\n" + ",\n".join(parts) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(x, (dict, list)) for x in value):
            inline = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
            if len(inline) <= 120:
                return inline
        parts = [f"{child_pad}{_render(x, level + 1, indent=indent)}" for x in value]
        return "[\n" + ",\n".join(parts) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def dumps_compact(config: dict) -> str:
    return _render(config, 0, indent=2) + "\n"


def compact_config_file(path: str | Path) -> int:
    path = Path(path)
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    compacted, removed = compact_config(obj)
    rendered = dumps_compact(compacted)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(path)
    return removed
