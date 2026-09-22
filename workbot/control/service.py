from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any


class WorkBotControlService:
    """Programmatic control plane over one live :class:`WorkBot` instance.

    The GUI intentionally talks to this service instead of reaching into Tk
    callbacks directly.  This keeps runtime operations reusable by a future web
    UI while preserving WorkBot/SQLite as the only authoritative state.
    """

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _row(row) -> dict[str, Any]:
        return dict(row) if row is not None else {}

    async def snapshot(self) -> dict[str, Any]:
        bot = self.bot
        now = time.time()
        scheduler = bot.agents.scheduler_stats()
        invocations = bot.agents.active_agent_sessions()
        agent_runs = []
        for state in bot.agents.backend.list_runs()[:100]:
            agent_runs.append({
                "run_id": state.run_id,
                "conversation_id": state.conversation_id,
                "session_id": state.session_id or "",
                "task_id": state.task_id or "",
                "workflow_id": state.workflow_id or "",
                "pid": state.pid,
                "status": state.status,
                "started_at": state.started_at,
                "ended_at": state.ended_at,
                "elapsed": state.elapsed,
                "last_output_at": state.last_output_at,
                "last_output_age": (None if state.last_output_at is None else max(0.0, now - state.last_output_at)),
                "exit_code": state.exit_code,
                "error": state.error or "",
                "recent_output": list(state.recent_output),
            })
        tasks = [dict(r) for r in bot.store.query_all(
            "SELECT * FROM tasks ORDER BY updated_at DESC, created_at DESC LIMIT 200"
        )]
        workflows = [dict(r) for r in bot.store.query_all(
            "SELECT * FROM workflows ORDER BY updated_at DESC, created_at DESC LIMIT 100"
        )]
        approvals = [dict(r) for r in bot.store.query_all(
            "SELECT * FROM approval_requests ORDER BY created_at DESC LIMIT 100"
        )]
        conversations = [dict(r) for r in bot.store.query_all(
            "SELECT conversation_id,platform,kind,external_id,display_name,pending_action_json,updated_at "
            "FROM conversations ORDER BY updated_at DESC LIMIT 100"
        )]
        nodes = []
        for item in bot.nodes.all():
            row = asdict(item)
            row["load_ratio"] = item.load_ratio
            nodes.append(row)
        outbox = bot.store.query_one(
            "SELECT COUNT(*) AS pending, COALESCE(SUM(CASE WHEN state='sending' THEN 1 ELSE 0 END),0) AS sending "
            "FROM outbound_messages WHERE state!='delivered'"
        )
        rag_status = {}
        try:
            rag_status = dict(bot.rag.status())
        except Exception as exc:  # diagnostics must be fail-soft
            rag_status = {"error": str(exc)}
        realtime = bot.realtime
        transport = {
            "mode": getattr(bot, "_realtime_receive_mode", "polling"),
            "realtime_enabled": realtime is not None,
            "realtime_connected": bool(getattr(realtime, "connected", False)) if realtime is not None else False,
            "backfill_active": bool(getattr(bot.im, "backfill_active", False)),
        }
        return {
            "timestamp": now,
            "runtime": {
                "running": not bot.stop_event.is_set(),
                "config_path": str(bot.config_path),
                "workspace": str(bot.workspace),
            },
            "transport": transport,
            "scheduler": scheduler,
            "invocations": invocations,
            "agent_runs": agent_runs,
            "tasks": tasks,
            "workflows": workflows,
            "approvals": approvals,
            "conversations": conversations,
            "nodes": nodes,
            "rag": rag_status,
            "outbox": dict(outbox) if outbox else {"pending": 0, "sending": 0},
            "collaboration": {
                "enabled": bool(getattr(bot, "_collaboration_enabled", False)),
                "agent_id": str(getattr(bot, "_collaboration_agent_id", "")),
                "discovery_enabled": bool(getattr(bot, "_collaboration_discovery_enabled", False)),
                "last_discovery_at": getattr(bot, "_collaboration_last_discovery_at", None),
                "groups": sorted(getattr(bot, "_collaboration_groups", set())),
                "peers": bot.collaboration_peers_snapshot() if hasattr(bot, "collaboration_peers_snapshot") else [],
            },
        }

    async def action(self, name: str, **kwargs) -> dict[str, Any]:
        bot = self.bot
        action = str(name or "").strip().lower()
        if action == "config.reload":
            changed = bot._reload_hot_config_if_changed(force=True)
            return {"ok": True, "changed": changed}
        if action == "agent.stop":
            invocation_id = str(kwargs.get("invocation_id") or "")
            result = bot.agents.stop_agent_session(invocation_id)
            if result is None:
                raise KeyError(f"unknown/terminal invocation {invocation_id}")
            return {"ok": True, "result": result}
        if action == "agent.stop_all":
            result = bot.agents.scheduler.cancel_all()
            return {"ok": True, "result": result}
        if action == "task.cancel":
            task_id = str(kwargs.get("task_id") or "")
            return {"ok": True, "result": await bot.tasks.cancel(task_id)}
        if action == "task.retry":
            task_id = str(kwargs.get("task_id") or "")
            new_id = await bot.tasks.retry(task_id)
            return {"ok": True, "task_id": new_id}
        if action == "task.append":
            task_id = str(kwargs.get("task_id") or "")
            instruction = str(kwargs.get("instruction") or "").strip()
            return {"ok": True, "result": await bot.tasks.append(task_id, instruction)}
        if action == "task.steer":
            task_id = str(kwargs.get("task_id") or "")
            instruction = str(kwargs.get("instruction") or "").strip()
            return {"ok": True, "result": await bot.tasks.steer(task_id, instruction)}
        if action == "workflow.cancel":
            workflow_id = str(kwargs.get("workflow_id") or "")
            await bot._cancel_workflow(workflow_id, notify=False)
            return {"ok": True}
        if action == "workflow.resume":
            workflow_id = str(kwargs.get("workflow_id") or "")
            await bot._resume_blocked_workflow(workflow_id)
            return {"ok": True}
        if action == "workflow.retry_step":
            workflow_id = str(kwargs.get("workflow_id") or "")
            step_id = str(kwargs.get("step_id") or "")
            await bot._retry_workflow_step(workflow_id, step_id)
            return {"ok": True}
        if action == "rag.sync.start":
            return {"ok": True, "result": await bot.rag.start_sync_job()}
        if action == "rag.sync.stop":
            return {"ok": True, "result": bot.rag.stop_sync_job()}
        if action == "rag.embed.start":
            limit = kwargs.get("limit")
            all_chunks = bool(kwargs.get("all_chunks", False))
            return {"ok": True, "result": await bot.rag.start_embed_job(limit=limit, all_chunks=all_chunks)}
        if action == "rag.embed.stop":
            return {"ok": True, "result": bot.rag.stop_embed_job()}
        if action == "collaboration.discover":
            sent = await bot.discover_workbots()
            return {"ok": True, "sent": sent}
        if action == "collaboration.send":
            conversation_id = str(kwargs.get("conversation_id") or "")
            peer_id = str(kwargs.get("peer_id") or "")
            body = str(kwargs.get("body") or "").strip()
            message_id = await bot._send_collaboration_message(conversation_id, peer_id, body)
            return {"ok": True, "message_id": message_id}
        raise ValueError(f"unsupported control action: {name}")
