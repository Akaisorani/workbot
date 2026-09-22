from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any


class WindowsRPCManager:
    """Execute a small, allow-listed set of Linux-originated Windows requests.

    The RPC layer is intentionally read-only in V0.7.  Remote nodes do not get
    a generic Windows shell.  Direct filesystem methods are constrained by
    configured roots, while ``workbot.query`` is handled by a fresh read-only
    CodeAgent invocation and never reuses/mutates the IM Conversation session.
    """

    DEFAULT_METHODS = {"windows.fs.read", "windows.fs.list", "workbot.query"}

    def __init__(self, cfg: dict, workspace: Path, agents, conversations):
        self.cfg = cfg or {}
        self.workspace = workspace.resolve()
        self.agents = agents
        self.conversations = conversations
        self.enabled = bool(self.cfg.get("enabled", False))
        self.allowed_nodes = {str(x) for x in self.cfg.get("allowed_nodes", [])}
        self.allowed_methods = {str(x) for x in self.cfg.get("allowed_methods", sorted(self.DEFAULT_METHODS))}
        roots = self.cfg.get("windows_read_roots") or [str(self.workspace)]
        self.read_roots = [Path(os.path.expandvars(os.path.expanduser(str(x)))).resolve() for x in roots]
        self.max_response_chars = max(1000, int(self.cfg.get("max_response_chars", 16000)))
        self.max_file_chars = max(1000, int(self.cfg.get("max_file_chars", 64000)))
        self.timeout = max(5.0, float(self.cfg.get("timeout_seconds", 300)))
        self._sem = asyncio.Semaphore(max(1, int(self.cfg.get("max_concurrent", 2))))

    def _authorize(self, node: str, method: str) -> None:
        if not self.enabled:
            raise PermissionError("Linux-to-Windows RPC is disabled")
        if self.allowed_nodes and node not in self.allowed_nodes:
            raise PermissionError(f"RPC node {node!r} is not allowed")
        if method not in self.allowed_methods:
            raise PermissionError(f"RPC method {method!r} is not allowed")

    def _resolve_read_path(self, raw: str) -> Path:
        if not raw or "\x00" in raw:
            raise ValueError("invalid path")
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = self.workspace / path
        resolved = path.resolve()
        for root in self.read_roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise PermissionError(f"path is outside configured Windows read roots: {resolved}")

    async def execute(self, node: str, method: str, data: dict[str, Any], *,
                      task_id: str | None = None) -> dict[str, Any]:
        self._authorize(node, method)
        async with self._sem:
            try:
                result = await asyncio.wait_for(
                    self._execute_inner(node, method, data or {}, task_id=task_id), timeout=self.timeout
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"RPC {method} timed out after {self.timeout:g}s") from exc
        return self._bounded_result(result)

    async def _execute_inner(self, node: str, method: str, data: dict[str, Any], *,
                             task_id: str | None) -> dict[str, Any]:
        if method == "windows.fs.read":
            path = self._resolve_read_path(str(data.get("path") or ""))
            if not path.is_file():
                raise FileNotFoundError(str(path))
            text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
            truncated = len(text) > self.max_file_chars
            if truncated:
                text = text[:self.max_file_chars]
            return {"path": str(path), "content": text, "truncated": truncated}

        if method == "windows.fs.list":
            path = self._resolve_read_path(str(data.get("path") or "."))
            if not path.is_dir():
                raise NotADirectoryError(str(path))
            max_entries = max(1, min(int(data.get("max_entries") or 200), 1000))
            all_children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            entries = []
            for child in all_children[:max_entries]:
                try:
                    stat = child.stat()
                    size = stat.st_size
                except OSError:
                    size = None
                entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir(), "size": size})
            return {"path": str(path), "entries": entries, "truncated": len(all_children) > max_entries}

        if method == "workbot.query":
            instruction = str(data.get("instruction") or data.get("query") or "").strip()
            if not instruction:
                raise ValueError("workbot.query requires instruction")
            if len(instruction) > 12000:
                raise ValueError("workbot.query instruction is too long")
            conversation_id = str(data.get("conversation_id") or "").strip() or None
            if conversation_id and not self.conversations.get(conversation_id):
                # A node may pass a stale or user-entered id. Do not invent a new
                # Conversation just to service an RPC request.
                conversation_id = None
            text = await self.agents.answer_node_request(
                node=node,
                instruction=instruction,
                conversation_id=conversation_id,
                task_id=task_id,
            )
            return {"text": text}

        raise ValueError(f"unknown RPC method {method}")

    def _bounded_result(self, result: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(result, ensure_ascii=False)
        if len(raw) <= self.max_response_chars:
            return result
        # Prefer truncating the large human-readable fields while preserving
        # structured metadata such as the path/method result.
        out = dict(result)
        for key in ("text", "content"):
            if isinstance(out.get(key), str):
                keep = max(1000, self.max_response_chars - 1000)
                out[key] = out[key][:keep]
                out["truncated"] = True
                return out
        return {"truncated": True, "preview": raw[:self.max_response_chars]}
