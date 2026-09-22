from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import re
import signal
import time
import shutil
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from workbot.agents.manager import AgentManager
from workbot.agents.scheduler import AgentInvocationStopped
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage, PendingAction
from workbot.collaboration.protocol import (
    AgentEnvelope, decode_agent_message, encode_agent_message, new_agent_message_id,
    encode_discovery_body, decode_discovery_body,
)
from workbot.im.welink import WeLinkAdapter, WeLinkSendAmbiguousError, WeLinkTransientError
from workbot.im.welink_format import split_welink_markdown
from workbot.im.welinkbot import WeLinkBotReceiver
from workbot.im.rich_message import ImageContextProcessor
from workbot.memory.manager import MemoryManager
from workbot.rag.service import RAGService
from workbot.knowledge.workspace import WorkspaceKnowledgeManager
from workbot.nodes.registry import NodeRegistry
from workbot.orchestration.manager import WorkflowManager
from workbot.orchestration.models import WorkflowPlan, WorkflowStep
from workbot.orchestration.scope import ExecutionScope, LOCAL_NODE, resolve_execution_scope
from workbot.policy.engine import PolicyEngine
from workbot.policy.approvals import ApprovalManager
from workbot.rpc.manager import WindowsRPCManager
from workbot.storage.sqlite import Store
from workbot.tasks.manager import TaskManager
from workbot.tasks.routing import (
    configured_node_in_text,
    is_explicit_remote_action,
    looks_like_mixed_workflow,
    preview,
)
from workbot.transport.protocol import Message, new_id
from workbot.transport.ssh import SSHManager

log = logging.getLogger(__name__)

CONFIRM_WORDS = {"创建", "确认", "确定", "可以", "好", "好的", "执行", "开始", "是", "yes", "y", "ok", "/confirm"}
CANCEL_WORDS = {"取消", "不创建", "不要", "算了", "否", "no", "n", "/cancel"}
PENDING_CONFIRM_RE = re.compile(r"^\s*(创建|确认|确定|可以|好|好的|执行|开始|是|yes|y|ok)\s*[。！!]*\s*$", re.I)
PENDING_CANCEL_RE = re.compile(r"^\s*(取消|不创建|不要|算了|否|no|n)\s*[。！!]*\s*$", re.I)
TASK_ID_RE = re.compile(r"\btask-[0-9a-fA-F]{6,32}\b")
WF_ID_RE = re.compile(r"\bwf-[0-9a-fA-F]{6,32}\b")
STEP_ID_RE = re.compile(r"\bstep-[A-Za-z0-9_.-]+\b")
DELEGATED_TASK_RE = re.compile(
    r"(?:处理|执行|接手|继续处理)\s*(?:一下)?\s*(?:上面|上面的|刚才|之前)\s*(?:来自\s*)?"
    r"(?P<sender>[A-Za-z][A-Za-z0-9_.:-]{4,})\s*(?:的)?\s*(?:任务|请求)",
    re.IGNORECASE,
)
QUOTE_EXECUTION_RE = re.compile(
    r"(?:帮我)?\s*(?:处理|执行|接手|继续处理|跑|运行|完成)\s*(?:一下)?\s*"
    r"(?:这个|这条|该条|引用的|被引用的|上面的|这条引用)?\s*(?:消息|任务|请求|内容)?\s*$",
    re.IGNORECASE,
)

KNOWN_SLASH_COMMAND_PATTERNS = (
    re.compile(r"^/(?:status|session|sessions|memories|nodes|welink|welink-tools|workspace|rag|approvals|newsession|reset-session|summary-refresh|summary-reset|confirm|cancel)$", re.IGNORECASE),
    re.compile(r"^/agent\s+(?:status|peers|discover|tail(?:\s+\d{1,3})?|send\s+[A-Za-z0-9_.:-]{1,128}\s+\S[\s\S]*)$", re.IGNORECASE),
    re.compile(r"^/rag\s+(?:status|reindex)$", re.IGNORECASE),
    re.compile(r"^/rag\s+sync(?:\s+(?:status|stop))?$", re.IGNORECASE),
    re.compile(r"^/rag\s+embed(?:\s+(?:\d+|all|status|stop))?$", re.IGNORECASE),
    re.compile(r"^/rag\s+search\s+\S[\s\S]*$", re.IGNORECASE),
    re.compile(r"^/summary\s+(?:refresh|reset)$", re.IGNORECASE),
    re.compile(r"^/session\s+set\s+[0-9a-fA-F-]{36}$", re.IGNORECASE),
    re.compile(r"^/(?:memory|remember|task|workflow|test)\s+\S[\s\S]*$", re.IGNORECASE),
    re.compile(r"^/workspace\s+\S[\s\S]*$", re.IGNORECASE),
    re.compile(r"^/forget\s+(?:auto|auto-summary|summary-auto|#?\d+)$", re.IGNORECASE),
    re.compile(r"^/(?:approve|deny)\s+approval-[0-9a-fA-F]{6,32}(?:\s+[\s\S]+)?$", re.IGNORECASE),
    re.compile(r"^/stop-session\s+\S+$", re.IGNORECASE),
    re.compile(r"^/stop-sessions(?:\s+all)?$", re.IGNORECASE),
    re.compile(r"^/cancel(?:\s+(?:task|wf)-[0-9a-fA-F]{6,32})?$", re.IGNORECASE),
    re.compile(r"^/retry(?:\s+(?:(?:task|wf)-[0-9a-fA-F]{6,32})(?:\s+step-[A-Za-z0-9_.-]+)?)?$", re.IGNORECASE),
    re.compile(r"^/resume(?:\s+wf-[0-9a-fA-F]{6,32})?$", re.IGNORECASE),
    re.compile(r"^/continue\s+task-[0-9a-fA-F]{6,32}(?:\s+[\s\S]+)?$", re.IGNORECASE),
    re.compile(r"^/(?:steer|add)\s+task-[0-9a-fA-F]{6,32}\s+\S[\s\S]*$", re.IGNORECASE),
    re.compile(r"^/(?:steer|add)\s+wf-[0-9a-fA-F]{6,32}\s+step-[A-Za-z0-9_.-]+\s+\S[\s\S]*$", re.IGNORECASE),
)



class WorkBot:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.workspace = self.config_path.parent.parent.resolve()
        self.cfg = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        self._config_file_signature = self._config_signature()
        self._config_reload_interval = max(0.5, float(self.cfg.get("config_reload_interval_seconds", 2.0)))
        # V1.12.2: keep local AGENTS.md synchronized with an allowlisted
        # config context. Never overwrite a hand-maintained legacy AGENTS.md
        # during normal startup; install/configure performs the explicit backup.
        try:
            from workbot.setup.agents_generator import AUTO_MARKER, generate_agents_file, needs_regeneration
            template = self.workspace / "AGENTS.example.md"
            agents_file = self.workspace / "AGENTS.md"
            if template.exists():
                if not agents_file.exists():
                    generate_agents_file(template, self.cfg, agents_file, backup_manual=False)
                else:
                    head = agents_file.read_text(encoding="utf-8", errors="replace")[:1200]
                    if AUTO_MARKER in head and needs_regeneration(template, self.cfg, agents_file):
                        generate_agents_file(template, self.cfg, agents_file, backup_manual=False)
                    elif AUTO_MARKER not in head:
                        log.warning("Legacy/manual AGENTS.md detected; startup will not overwrite it. Run scripts/generate-agents.ps1 to migrate explicitly.")
        except Exception:
            log.exception("AGENTS.md startup generation check failed; continuing with existing workspace context")
        db_path = Path(self.cfg.get("database", "state/workbot.db"))
        if not db_path.is_absolute():
            db_path = self.workspace / db_path
        self.store = Store(db_path)
        # V1.8.1: an interrupted direct send may leave an outbox row in
        # ``sending``. Re-queue it so the durable retry loop can verify history
        # before sending again on startup.
        self.store.execute("UPDATE outbound_messages SET state='pending' WHERE state='sending'")
        self.conversations = ConversationManager(self.store)
        self.memory_cfg = self.cfg.get("memory", {})
        self.memory = MemoryManager(self.store, self.memory_cfg)
        self.action_policy_cfg = self.cfg.get("action_policy", {})
        # V1.2 separates passive ingestion, read-only reply permission and
        # execution/approval permission. access_control remains a compatibility
        # fallback for existing V1.1 installations.
        legacy_acl = self.cfg.get("access_control", {})
        self.ingestion_policy = PolicyEngine(self.cfg.get("ingestion_policy", legacy_acl), {})
        self.reply_policy = PolicyEngine(self.cfg.get("reply_policy", legacy_acl), {})
        self.policy = PolicyEngine(self.cfg.get("execution_policy", legacy_acl), self.action_policy_cfg)
        self.approvals = ApprovalManager(self.store, self.action_policy_cfg)
        self.workflows = WorkflowManager(self.store)

        im_cfg = self.cfg["im"]
        welink_tools_cfg = dict(self.cfg.get("welink_tools", {}) or {})
        shared_gate_path = str(self.workspace / welink_tools_cfg.get("shared_gate_path", "state/welink-cli.gate"))
        shared_rate_path = str(self.workspace / welink_tools_cfg.get("shared_rate_path", "state/welink-rate.json"))
        discovery_cfg = dict(im_cfg.get("discovery", {}) or {})
        intent_cfg = im_cfg.get("intent", {}) or {}
        discovery_cfg.setdefault("priority_groups", list(intent_cfg.get("important_groups", []) or []))
        configured_self_accounts = [str(x) for x in (im_cfg.get("self_accounts") or []) if str(x)]
        execution_allow = [str(x) for x in (self.cfg.get("execution_policy", legacy_acl).get("allow_senders", []) or []) if str(x)]
        if not configured_self_accounts and len(execution_allow) == 1:
            configured_self_accounts = [execution_allow[0]]
            log.info("Inferred local WeLink self account from execution_policy.allow_senders: %s", execution_allow[0])
        self._operator_self_accounts = set(configured_self_accounts)
        self.im = WeLinkAdapter(
            cli=im_cfg.get("cli", "welink-cli"),
            groups=im_cfg.get("groups", []),
            reply_prefix=self.cfg.get("bot_reply_prefix", "[AGENT]"),
            bootstrap_from_latest=bool(im_cfg.get("bootstrap_from_latest", True)),
            transient_backoff_seconds=float(im_cfg.get("transient_backoff_seconds", 30)),
            transient_backoff_max_seconds=float(im_cfg.get("transient_backoff_max_seconds", 300)),
            send_verify_delay_seconds=float(im_cfg.get("send_verify_delay_seconds", 1.5)),
            cli_trace=bool(im_cfg.get("cli_trace", False)),
            cli_timeout_seconds=float(im_cfg.get("cli_timeout_seconds", 90)),
            discovery=discovery_cfg,
            self_accounts=configured_self_accounts,
            shared_gate_path=shared_gate_path,
            shared_rate_path=shared_rate_path,
            media_search_roots=list(((im_cfg.get("rich_message") or {}).get("image") or {}).get("allowed_roots", []) or []),
            quote_enabled=bool(((im_cfg.get("rich_message") or {}).get("quote") or {}).get("enabled", True)),
            quote_max_chars=int(((im_cfg.get("rich_message") or {}).get("quote") or {}).get("max_chars", 10000)),
        )
        self.image_context = ImageContextProcessor(im_cfg.get("rich_message", {}), self_accounts=configured_self_accounts)
        realtime_cfg = dict(im_cfg.get("realtime", {}) or {})
        self.realtime = WeLinkBotReceiver(realtime_cfg, self.im.normalize_push_event) if bool(realtime_cfg.get("enabled", False)) else None
        self._realtime_reconcile_interval = max(30.0, float(realtime_cfg.get("reconcile_interval_seconds", 300)))
        self._realtime_fallback_interval = max(1.0, float(realtime_cfg.get("fallback_poll_interval_seconds", self.cfg.get("poll_interval_seconds", 3))))
        self._realtime_backfill_max_seconds = max(10.0, float(realtime_cfg.get("backfill_max_seconds", realtime_cfg.get("backfill_after_connect_seconds", 120))))
        self._realtime_backfill_until = 0.0
        self._realtime_next_cli_poll = 0.0
        self._realtime_receive_mode = "polling" if self.realtime is None else "connecting"
        for group in im_cfg.get("groups", []):
            cid = f"welink:group:{group['group_id']}"
            row = self.conversations.get(cid)
            if row and row["last_message_id"]:
                ts = self.store.query_one("SELECT MAX(sent_at) AS ts FROM messages WHERE conversation_id=?", (cid,))
                sent_at = int(ts["ts"] or 0) if ts else 0
                if str(row["last_message_id"]).isdigit():
                    self.im.set_last_seen(str(group["group_id"]), int(row["last_message_id"]), sent_at_ms=sent_at)
                elif sent_at:
                    self.im.set_last_seen_time(str(group["group_id"]), sent_at)
        # Dynamic discovery conversations also resume their ID/time cursors after restart.
        for row in self.store.query_all("SELECT conversation_id,kind,external_id,last_message_id FROM conversations WHERE platform='welink' AND last_message_id IS NOT NULL"):
            kind = "group" if str(row["kind"]) == "group" else "user"
            ts = self.store.query_one("SELECT MAX(sent_at) AS ts FROM messages WHERE conversation_id=?", (str(row["conversation_id"]),))
            sent_at = int(ts["ts"] or 0) if ts else 0
            if str(row["last_message_id"]).isdigit():
                self.im.set_last_seen(str(row["external_id"]), int(row["last_message_id"]), kind=kind, sent_at_ms=sent_at)
            elif sent_at:
                self.im.set_last_seen_time(str(row["external_id"]), sent_at, kind=kind)

        self.tasks = TaskManager(self.store)
        self.nodes = NodeRegistry(self.cfg.get("nodes", {}), self.store, default_node=self.cfg.get("default_node"))
        self.ssh = SSHManager(self.cfg.get("nodes", {}), self.on_remote_message, self.nodes.set_connected)
        self.tasks.bind_transport(self.ssh)
        self.agents = AgentManager(self.cfg.get("agent", {}), self.workspace, self.conversations, self.memory)
        self.agents.set_node_context_provider(self.nodes.format_for_agent)
        self.agents.set_managed_node_names(self.nodes.configs.keys())
        if bool(welink_tools_cfg.get("managed_gateway", True)):
            raw_cli = str(im_cfg.get("cli", "welink-cli"))
            real_cli = shutil.which(raw_cli) or raw_cli
            self.agents.configure_managed_welink(
                real_cli=real_cli, gate_path=shared_gate_path, rate_path=shared_rate_path,
                trace=bool(welink_tools_cfg.get("gateway_trace", False)),
            )
        self.rpc = WindowsRPCManager(self.cfg.get("rpc", {}), self.workspace, self.agents, self.conversations)
        self.workspace_knowledge = WorkspaceKnowledgeManager(
            self.store, self.memory, self.cfg.get("workspace_knowledge", {}), self.rpc.read_roots,
            node_configs=self.cfg.get("nodes", {}), node_request=self.tasks.request_node,
            node_online=self.nodes.is_connected,
        )
        self.agents.set_workspace_knowledge(self.workspace_knowledge)
        # V1.11 unified local Hybrid RAG. The service is deliberately optional at
        # the dependency level: without sqlite-vec/sentence-transformers the
        # existing FTS/LIKE paths keep WorkBot usable while /rag status explains
        # what is missing.
        self.rag = RAGService(self.store, self.cfg.get("rag", {}))
        self.agents.set_rag_service(self.rag)
        self.stop_event = asyncio.Event()
        self._background: set[asyncio.Task] = set()
        self._workflow_runners: dict[str, asyncio.Task] = {}
        # V1.3 Windows workflow-step steering. The outer workflow step remains
        # alive while only the current CodeAgent invocation is cancelled/resumed.
        self._windows_step_agent_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._windows_step_instruction_queues: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._conversation_queues: dict[str, asyncio.Queue[IncomingMessage]] = {}
        self._conversation_workers: dict[str, asyncio.Task] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._summary_inflight: set[str] = set()
        self._memory_inflight: set[str] = set()
        self._agent_failure_notifications: dict[str, tuple[str, float]] = {}
        self._agent_failure_cooldown = float(self.cfg.get("agent_failure_cooldown_seconds", 60))
        self._workflow_step_timeout = float(self.cfg.get("workflow_step_timeout_seconds", 3600))
        conv_cfg = self.cfg.get("conversation", {})
        self._summary_every = int(conv_cfg.get("summary_every_messages", 12))
        self._summary_min = int(conv_cfg.get("summary_min_messages", 8))
        out_cfg = self.cfg.get("outbound", {})
        self._outbound_retry_base = max(2, int(out_cfg.get("retry_seconds", 15)))
        self._outbound_retry_max = max(self._outbound_retry_base, int(out_cfg.get("retry_max_seconds", 300)))
        # V1.10: split logical replies before durable delivery so WeLink never
        # receives an oversized message or more than one native code block.
        self._welink_max_message_chars = max(512, int(out_cfg.get("welink_max_message_chars", 3500)))
        intent_cfg = im_cfg.get("intent", {})
        self._intent_enabled = bool(intent_cfg.get("enabled", False))
        self._bot_aliases = [str(x) for x in intent_cfg.get("bot_aliases", ["@WorkBot", "WorkBot", "workbot"]) if str(x)]
        # V1.6 strict opt-in dispatch. This gate is intentionally independent
        # from ingestion_policy: unaddressed messages are still durably stored.
        self._require_bot_alias = bool(intent_cfg.get("require_alias", False))
        self._important_groups = {str(x) for x in intent_cfg.get("important_groups", [])}
        self._ambient_delay = max(5.0, float(intent_cfg.get("ambient_batch_delay_seconds", 60)))
        self._important_ambient_delay = max(2.0, float(intent_cfg.get("important_batch_delay_seconds", 12)))
        self._ambient_max_batch = max(2, int(intent_cfg.get("max_batch_messages", 20)))
        self._ambient_max_scheduler_waiting = max(0, int(intent_cfg.get("max_pending_agent_batches", 4)))
        self._ambient_batches: dict[str, list[IncomingMessage]] = {}
        self._ambient_flushers: dict[str, asyncio.Task] = {}
        self._ambient_summary_every = max(20, int(intent_cfg.get("ambient_summary_every_messages", 100)))
        self._silent_unfulfillable = bool(self.cfg.get("reply_policy", {}).get("silent_if_unfulfillable", True))

        # V1.12.3 multi-WorkBot group collaboration. Peer identity is bound to
        # the real IM sender account; the human-visible [AGENT] prefix is never
        # trusted as identity or authorization.
        collab_cfg = dict(self.cfg.get("collaboration", {}) or {})
        self._collaboration_enabled = bool(collab_cfg.get("enabled", False))
        self._collaboration_agent_id = str(collab_cfg.get("agent_id") or "").strip()
        if not self._collaboration_agent_id and configured_self_accounts:
            # Keep setup lightweight: peers need no static config, and even the
            # local id can be derived deterministically from the real IM account.
            raw_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", configured_self_accounts[0]).strip("-.")
            self._collaboration_agent_id = f"workbot-{raw_id[-24:]}" if raw_id else ""
        configured_collab_groups = {str(x) for x in (collab_cfg.get("groups") or []) if str(x)}
        if configured_collab_groups:
            self._collaboration_groups = configured_collab_groups
        else:
            # When collaboration is explicitly enabled, an empty groups list
            # means the configured WeLink groups. This removes one more piece of
            # duplicated setup while still avoiding arbitrary discovered groups.
            self._collaboration_groups = {
                str(x.get("group_id")) for x in (im_cfg.get("groups") or [])
                if isinstance(x, dict) and str(x.get("group_id") or "")
            }
        self._collaboration_accept_broadcast = bool(collab_cfg.get("accept_broadcast", False))
        self._collaboration_max_hops = max(1, min(16, int(collab_cfg.get("max_hops", 4))))
        # Discovery is passive until a human explicitly triggers it.  The old
        # auto_discovery key remains a compatibility alias for whether this
        # instance participates in handshakes; it no longer starts a timer.
        self._collaboration_discovery_enabled = bool(
            collab_cfg.get("discovery_enabled", collab_cfg.get("auto_discovery", True))
        )
        self._collaboration_peer_ttl = max(60.0, float(collab_cfg.get("peer_ttl_seconds", 300)))
        self._collaboration_display_name = str(collab_cfg.get("display_name") or (self._bot_aliases[0] if self._bot_aliases else self._collaboration_agent_id)).strip()
        self._collaboration_peers = {
            str(peer_id): dict(peer_cfg or {})
            for peer_id, peer_cfg in (collab_cfg.get("peers") or {}).items()
            if str(peer_id).strip() and isinstance(peer_cfg, dict)
        }
        self._collaboration_discovered: dict[str, dict] = {}
        self._collaboration_last_discovery_at: float | None = None
        self._collaboration_seen_order: list[str] = []
        self._collaboration_seen: set[str] = set()
        self._collaboration_reply_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
            "workbot_collaboration_reply", default=None
        )
        if self._collaboration_enabled and not self._collaboration_agent_id:
            log.warning("collaboration.enabled=true but no agent_id can be derived; collaboration dispatch is disabled")
            self._collaboration_enabled = False
        if self._collaboration_enabled and not configured_self_accounts:
            log.warning("collaboration requires im.self_accounts so peer [AGENT] messages can be distinguished from local echoes; collaboration dispatch is disabled")
            self._collaboration_enabled = False
        if self._collaboration_enabled and not self._collaboration_groups:
            log.warning("collaboration.enabled=true but no collaboration/im group is configured; collaboration dispatch is disabled")
            self._collaboration_enabled = False
        self.agents.set_collaboration_context_provider(self._collaboration_context_for_agent)

        maint = self.cfg.get("maintenance", {})
        self._maintenance_interval = max(30, int(maint.get("interval_seconds", 120)))
        self._memory_batch_messages = max(20, int(self.memory_cfg.get("conversation_batch_messages", 60)))
        self._promotion_min_candidates = max(1, int(self.memory_cfg.get("promotion_min_candidates", 4)))
        self._promotion_night_start = int(self.memory_cfg.get("promotion_night_start_hour", 1))
        self._promotion_night_end = int(self.memory_cfg.get("promotion_night_end_hour", 5))
        knowledge_cfg = dict(self.cfg.get("workspace_knowledge", {}) or {})
        self._knowledge_idle_grace = max(0, int(knowledge_cfg.get("idle_grace_seconds", 300)))
        self._knowledge_night_start = int(knowledge_cfg.get("night_start_hour", 1))
        self._knowledge_night_end = int(knowledge_cfg.get("night_end_hour", 6))
        self._last_interactive_activity = time.monotonic()

    def _config_signature(self) -> tuple[int, int] | None:
        try:
            st = self.config_path.stat()
            return int(st.st_mtime_ns), int(st.st_size)
        except OSError:
            return None

    def _reload_hot_config_if_changed(self, *, force: bool = False) -> list[str]:
        """Atomically hot-reload the explicitly supported policy sections.

        Other configuration remains restart-bound by design. This avoids a
        dangerous half-reloaded runtime where, for example, ``nodes`` changes in
        ``self.cfg`` while SSHManager/TaskManager still hold the old topology.
        On JSON/policy validation failure the last known-good policy objects stay
        active.
        """
        signature = self._config_signature()
        if not force and signature == self._config_file_signature:
            return []
        # Mark this file version as observed even when invalid, so a partially
        # written/manual edit does not flood logs every reload interval. A later
        # save changes the signature and is retried automatically.
        self._config_file_signature = signature
        try:
            fresh = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(fresh, dict):
                raise ValueError("config root must be a JSON object")
            old_legacy = self.cfg.get("access_control", {})
            new_legacy = fresh.get("access_control", old_legacy)
            ingestion_cfg = fresh.get("ingestion_policy", new_legacy)
            reply_cfg = fresh.get("reply_policy", new_legacy)
            execution_cfg = fresh.get("execution_policy", new_legacy)
            # Construct every replacement first. If any one is invalid, none of
            # the three is swapped: the hot reload is all-or-nothing.
            new_ingestion = PolicyEngine(ingestion_cfg, {})
            new_reply = PolicyEngine(reply_cfg, {})
            new_execution = PolicyEngine(execution_cfg, self.action_policy_cfg)
        except Exception as exc:
            log.warning("Config hot reload rejected; keeping last known-good policies: %s", exc)
            return []

        changed: list[str] = []
        comparisons = (
            ("ingestion_policy", self.cfg.get("ingestion_policy", old_legacy), ingestion_cfg),
            ("reply_policy", self.cfg.get("reply_policy", old_legacy), reply_cfg),
            ("execution_policy", self.cfg.get("execution_policy", old_legacy), execution_cfg),
        )
        for key, old, new in comparisons:
            if old != new:
                changed.append(key)

        # access_control is retained only as the legacy fallback source. It is
        # safe to update this snapshot because no subsystem reads it directly at
        # runtime after PolicyEngine construction.
        if fresh.get("access_control", old_legacy) != old_legacy:
            self.cfg["access_control"] = fresh.get("access_control", old_legacy)
        if "ingestion_policy" in fresh:
            self.cfg["ingestion_policy"] = fresh["ingestion_policy"]
        elif "ingestion_policy" in self.cfg:
            self.cfg.pop("ingestion_policy", None)
        if "reply_policy" in fresh:
            self.cfg["reply_policy"] = fresh["reply_policy"]
        elif "reply_policy" in self.cfg:
            self.cfg.pop("reply_policy", None)
        if "execution_policy" in fresh:
            self.cfg["execution_policy"] = fresh["execution_policy"]
        elif "execution_policy" in self.cfg:
            self.cfg.pop("execution_policy", None)

        self.ingestion_policy = new_ingestion
        self.reply_policy = new_reply
        self.policy = new_execution
        self._silent_unfulfillable = bool((reply_cfg or {}).get("silent_if_unfulfillable", True))
        if changed:
            log.info("Config hot reload applied: %s", ", ".join(changed))
        else:
            log.info("Config file changed; no supported hot-reload policy section changed")
        return changed

    async def _config_reload_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._reload_hot_config_if_changed()
            except Exception:
                log.exception("Unexpected config hot reload failure; keeping current runtime policies")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self._config_reload_interval)
            except asyncio.TimeoutError:
                pass

    def _collaboration_peer_config(self, peer_id: str) -> dict | None:
        """Return one trusted peer binding, preferring explicit static config.

        Auto-discovered peers are runtime state learned from signed-in IM sender
        identities inside collaboration groups. Static peers remain supported as
        an optional pin/override, but are no longer required.
        """
        peer_id = str(peer_id or "").strip()
        static = self._collaboration_peers.get(peer_id)
        if static is not None:
            item = dict(static)
            item.setdefault("source", "static")
            discovered = self._collaboration_discovered.get(peer_id)
            if discovered and not item.get("sender_accounts"):
                item["sender_accounts"] = list(discovered.get("sender_accounts") or [])
                item.setdefault("last_seen", discovered.get("last_seen"))
            return item
        discovered = self._collaboration_discovered.get(peer_id)
        return dict(discovered) if discovered else None

    def collaboration_peers_snapshot(self) -> list[dict]:
        ids = sorted(set(self._collaboration_peers) | set(self._collaboration_discovered))
        rows: list[dict] = []
        for peer_id in ids:
            cfg = self._collaboration_peer_config(peer_id) or {}
            rows.append({
                "agent_id": peer_id,
                "source": str(cfg.get("source") or ("static" if peer_id in self._collaboration_peers else "discovered")),
                "sender_accounts": list(cfg.get("sender_accounts") or []),
                "aliases": list(cfg.get("aliases") or []),
                "display_name": str(cfg.get("display_name") or cfg.get("description") or ""),
                "capabilities": list(cfg.get("capabilities") or []),
                "last_seen": float(cfg.get("last_seen") or 0.0),
            })
        return rows

    def _register_discovered_peer(self, msg: IncomingMessage, envelope: AgentEnvelope, payload: dict) -> bool:
        if not self._collaboration_discovery_enabled:
            return False
        if envelope.from_id != str(payload.get("agent_id") or ""):
            log.warning("Peer discovery identity mismatch envelope=%s payload=%s", envelope.from_id, payload.get("agent_id"))
            return False
        if envelope.from_id == self._collaboration_agent_id or str(msg.sender_id) in self._operator_self_accounts:
            return False
        sender = str(msg.sender_id or "").strip()
        if not sender:
            return False
        static = self._collaboration_peers.get(envelope.from_id)
        if static:
            pinned = {str(x) for x in (static.get("sender_accounts") or []) if str(x)}
            if pinned and sender not in pinned:
                log.warning("Peer discovery rejected for pinned peer %s sender=%s expected=%s", envelope.from_id, sender, sorted(pinned))
                return False
        existing = self._collaboration_discovered.get(envelope.from_id)
        if existing:
            accounts = {str(x) for x in (existing.get("sender_accounts") or []) if str(x)}
            age = max(0.0, time.time() - float(existing.get("last_seen") or 0.0))
            if accounts and sender not in accounts and age < self._collaboration_peer_ttl:
                log.warning(
                    "Peer discovery conflict for %s: sender=%s existing=%s age=%.1fs; keeping current binding",
                    envelope.from_id, sender, sorted(accounts), age,
                )
                return False
        self._collaboration_discovered[envelope.from_id] = {
            "source": "discovered",
            "sender_accounts": [sender],
            "aliases": list(payload.get("aliases") or []),
            "display_name": str(payload.get("display_name") or ""),
            "description": str(payload.get("display_name") or ""),
            "capabilities": list(payload.get("capabilities") or []),
            "last_seen": time.time(),
        }
        log.info("Peer Agent discovered: id=%s sender=%s name=%s", envelope.from_id, sender, payload.get("display_name") or "-")
        return True

    def _touch_collaboration_peer(self, peer_id: str) -> None:
        row = self._collaboration_discovered.get(str(peer_id))
        if row is not None:
            row["last_seen"] = time.time()

    def _collaboration_context_for_agent(self) -> str:
        if not self._collaboration_enabled:
            return "- <peer-Agent collaboration disabled>"
        lines = [f"- local agent_id: {self._collaboration_agent_id}"]
        peers = self.collaboration_peers_snapshot()
        if not peers:
            lines.append("- peers: <none discovered yet>")
            return "\n".join(lines)
        lines.append("- peers:")
        for cfg in peers:
            peer_id = cfg["agent_id"]
            aliases = ", ".join(str(x) for x in cfg.get("aliases") or [])
            label = str(cfg.get("display_name") or "").strip()
            extras = [f"source={cfg.get('source')}"]
            if aliases:
                extras.append(f"aliases={aliases}")
            if label:
                extras.append(label)
            lines.append(f"  - {peer_id} — " + "; ".join(extras))
        return "\n".join(lines)

    def _collaboration_discovery_capabilities(self) -> list[str]:
        caps = ["chat", "codeagent", "tasks", "workflows"]
        if bool((self.cfg.get("rag") or {}).get("enabled", False)):
            caps.append("rag")
        return caps

    async def _send_collaboration_discovery(self, conversation_id: str, *, kind: str, to_id: str = "*", reply_to: str | None = None) -> str:
        message_id = new_agent_message_id()
        envelope = AgentEnvelope(
            version=1, from_id=self._collaboration_agent_id, to_id=to_id,
            message_type="event", message_id=message_id, reply_to=reply_to, hop=0,
        )
        body = encode_discovery_body(
            kind, agent_id=self._collaboration_agent_id, display_name=self._collaboration_display_name,
            aliases=self._bot_aliases, capabilities=self._collaboration_discovery_capabilities(),
        )
        await self._send_text(conversation_id, encode_agent_message(envelope, body), collaboration_wrap=False)
        return message_id

    async def discover_workbots(self, conversation_id: str | None = None) -> list[dict]:
        """Send one discovery hello per explicitly selected collaboration group.

        This method is only called by human-facing controls.  WorkBot never
        schedules it in the background, so startup and idle operation remain
        silent while inbound hello/hello_ack messages are still handled.
        """
        if not self._collaboration_enabled:
            raise RuntimeError("collaboration is disabled")
        if not self._collaboration_discovery_enabled:
            raise RuntimeError("collaboration discovery is disabled")
        if conversation_id is not None:
            prefix = "welink:group:"
            if not conversation_id.startswith(prefix):
                raise ValueError("peer discovery is only available in a WeLink group")
            group_id = conversation_id[len(prefix):]
            if group_id not in self._collaboration_groups:
                raise ValueError("the current group is not in collaboration.groups")
            targets = [(group_id, conversation_id)]
        else:
            targets = [(group_id, f"welink:group:{group_id}") for group_id in sorted(self._collaboration_groups)]
        sent = []
        for group_id, target in targets:
            message_id = await self._send_collaboration_discovery(target, kind="hello", to_id="*")
            sent.append({"group_id": group_id, "message_id": message_id})
        self._collaboration_last_discovery_at = time.time()
        return sent

    def _remember_collaboration_message_id(self, message_id: str) -> bool:
        """Return False for a previously seen collaboration protocol id."""
        message_id = str(message_id or "")
        if message_id in self._collaboration_seen:
            return False
        self._collaboration_seen.add(message_id)
        self._collaboration_seen_order.append(message_id)
        if len(self._collaboration_seen_order) > 2048:
            old = self._collaboration_seen_order.pop(0)
            self._collaboration_seen.discard(old)
        return True

    def _route_collaboration_message(self, msg: IncomingMessage) -> tuple[bool, IncomingMessage | None]:
        """Recognize and securely route a peer WorkBot protocol message.

        Returns ``(recognized, dispatch_message)``. Recognized but non-targeted,
        untrusted, duplicate, response, and event messages are store-only. Only
        an explicitly targeted verified ``request`` bypasses the normal alias
        gate and invokes this WorkBot.
        """
        parsed = decode_agent_message(msg.content)
        if parsed is None:
            return False, None
        envelope, body = parsed
        if not self._collaboration_enabled or msg.conversation_kind != "group":
            return True, None
        if self._collaboration_groups and msg.external_conversation_id not in self._collaboration_groups:
            log.info("Peer Agent message store-only: group %s is outside collaboration.groups", msg.external_conversation_id)
            return True, None
        # Discovery/handshake is the one protocol path allowed before a peer
        # has a static binding. The actual WeLink sender is used as the binding
        # (TOFU within the explicitly configured collaboration group); the body
        # never authenticates itself.
        discovery = decode_discovery_body(body) if envelope.message_type == "event" else None
        if discovery is not None:
            if envelope.to_id not in {"*", self._collaboration_agent_id}:
                return True, None
            if not self._remember_collaboration_message_id(envelope.message_id):
                return True, None
            accepted = self._register_discovered_peer(msg, envelope, discovery)
            if accepted and discovery["kind"] == "hello":
                self._spawn(
                    self._send_collaboration_discovery(
                        msg.conversation_id, kind="hello_ack", to_id=envelope.from_id,
                        reply_to=envelope.message_id,
                    ),
                    name=f"peer-hello-ack:{envelope.from_id}",
                )
            return True, None

        peer_cfg = self._collaboration_peer_config(envelope.from_id)
        if not peer_cfg:
            log.info("Unknown peer Agent %s from sender %s; awaiting discovery handshake (store-only)", envelope.from_id, msg.sender_id)
            return True, None
        sender_accounts = {str(x) for x in (peer_cfg.get("sender_accounts") or []) if str(x)}
        if not sender_accounts or str(msg.sender_id) not in sender_accounts:
            log.warning(
                "Peer Agent identity mismatch: declared=%s sender=%s expected=%s; store-only",
                envelope.from_id, msg.sender_id, sorted(sender_accounts),
            )
            return True, None
        self._touch_collaboration_peer(envelope.from_id)
        target_ok = envelope.to_id == self._collaboration_agent_id
        if envelope.to_id == "*":
            target_ok = self._collaboration_accept_broadcast
        if not target_ok:
            return True, None
        if envelope.hop >= self._collaboration_max_hops and envelope.message_type == "request":
            log.warning("Peer Agent request %s exceeded max_hops=%s; store-only", envelope.message_id, self._collaboration_max_hops)
            return True, None
        if not self._remember_collaboration_message_id(envelope.message_id):
            log.info("Duplicate peer Agent protocol id %s suppressed", envelope.message_id)
            return True, None
        # Responses/events become durable group context but intentionally do not
        # trigger another automatic Agent turn. This is the primary ping-pong
        # loop breaker. A later human/local-Agent turn can still use them from
        # conversation history.
        if envelope.message_type != "request":
            log.info(
                "Peer Agent %s %s received from %s reply_to=%s (store-only)",
                envelope.message_type, envelope.message_id, envelope.from_id, envelope.reply_to or "-",
            )
            return True, None
        routed = replace(
            msg,
            content=body or "请确认已收到该协作请求。",
            agent_peer_id=envelope.from_id,
            agent_message_id=envelope.message_id,
            agent_message_type=envelope.message_type,
            agent_reply_to=envelope.reply_to or "",
            agent_hop=envelope.hop,
        )
        # The raw protocol text was already durably ingested. Persist the
        # verified peer metadata into context_json without rewriting content.
        try:
            self.conversations.update_message_context(routed)
        except Exception:
            log.exception("Failed to persist verified peer Agent metadata for %s", envelope.message_id)
        log.info(
            "Verified peer Agent request from=%s to=%s id=%s sender=%s",
            envelope.from_id, envelope.to_id, envelope.message_id, msg.sender_id,
        )
        return True, routed

    async def _send_collaboration_message(self, conversation_id: str, peer_id: str, body: str, *,
                                          message_type: str = "request", reply_to: str | None = None, hop: int = 0) -> str:
        peer_id = str(peer_id or "").strip()
        if not self._collaboration_enabled:
            raise RuntimeError("collaboration is disabled")
        if peer_id != "*" and self._collaboration_peer_config(peer_id) is None:
            raise ValueError(f"unknown collaboration peer: {peer_id}; run /agent discover or configure a static peer")
        if peer_id == "*" and not self._collaboration_accept_broadcast:
            raise ValueError("broadcast collaboration is disabled")
        message_id = new_agent_message_id()
        envelope = AgentEnvelope(
            version=1, from_id=self._collaboration_agent_id, to_id=peer_id,
            message_type=message_type, message_id=message_id, reply_to=reply_to, hop=int(hop),
        )
        payload = encode_agent_message(envelope, body)
        await self._send_text(conversation_id, payload, collaboration_wrap=False)
        return message_id

    def _create_welink_approval_token(self, action: str) -> str | None:
        if action not in {"im.send","im.write","email.send","email.write","meeting.write","calendar.write","cloud.write","cloud.share","contact.write"}:
            return None
        token_dir = self.workspace / "state" / "welink-approval-tokens"
        token_dir.mkdir(parents=True, exist_ok=True)
        path = token_dir / f"token-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({"action": action, "used": False, "created_at": time.time()}), encoding="utf-8")
        return str(path)

    async def run(self):
        await self.ssh.start()
        self._spawn(self._outbound_loop(), name="outbound-retry")
        self._spawn(self._maintenance_loop(), name="maintenance")
        self._spawn(self._config_reload_loop(), name="config-hot-reload")
        if self.realtime is not None:
            self._spawn(self.realtime.run(self.stop_event), name="welinkbot-realtime")
        await self._recover_workflows()
        if self.realtime is None:
            log.info("WorkBot started; WeLink receive transport=CLI polling every %ss", self.cfg.get("poll_interval_seconds", 3))
        else:
            log.info(
                "WorkBot started; WeLink receive transport=WeLinkBot WebSocket primary (%s), "
                "CLI history fallback/reconciliation enabled",
                self.realtime.url,
            )
        try:
            if self.realtime is None:
                await self._run_polling_im_loop()
            else:
                await self._run_realtime_im_loop()
        finally:
            for task in list(self._background):
                task.cancel()
            if self._background:
                await asyncio.gather(*list(self._background), return_exceptions=True)
            await self.ssh.stop()

    async def _ingest_dispatch_batch(self, messages: list[IncomingMessage]) -> None:
        # Ingest the whole batch first so durable message order is established
        # before any fast-lane control handlers start executing. Push/history
        # duplicates collapse through ConversationManager's unique key.
        accepted: list[IncomingMessage] = []
        for msg in sorted(messages, key=lambda m: (int(m.sent_at_ms or 0), str(m.external_message_id))):
            if await self._ingest_im_message(msg):
                accepted.append(msg)
        if accepted:
            accepted = list(await asyncio.gather(*(self._enrich_rich_message(msg) for msg in accepted)))
        for msg in accepted:
            self._dispatch_im_message(msg)

    async def _run_polling_im_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self._ingest_dispatch_batch(await self.im.poll())
            except Exception:
                log.exception("WeLink poll/dispatch failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=float(self.cfg.get("poll_interval_seconds", 3)))
            except asyncio.TimeoutError:
                pass

    async def _run_realtime_im_loop(self) -> None:
        assert self.realtime is not None
        next_cli_poll = 0.0
        backfill_deadline = 0.0
        self._realtime_next_cli_poll = 0.0
        self._realtime_backfill_until = 0.0
        seen_generation = 0
        was_connected = False
        while not self.stop_event.is_set():
            try:
                pushed = await self.realtime.get_batch(timeout=0.35, max_items=300)
                if pushed:
                    await self._ingest_dispatch_batch(pushed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WeLinkBot realtime dispatch failed")

            now = time.monotonic()
            connected = self.realtime.connected
            generation = self.realtime.backfill_generation
            if generation and generation != seen_generation:
                seen_generation = generation
                backfill_deadline = now + self._realtime_backfill_max_seconds
                self._realtime_backfill_until = backfill_deadline
                next_cli_poll = 0.0
                self._realtime_next_cli_poll = 0.0
                self.im.request_backfill()
                log.info(
                    "WeLinkBot connected/reconnected; starting bounded CLI history backfill sweep (max %.0fs) generation=%d",
                    self._realtime_backfill_max_seconds, generation,
                )
            if was_connected and not connected:
                # Push transport has failed. Switch to polling immediately rather
                # than waiting for the low-frequency healthy reconciliation tick.
                next_cli_poll = 0.0
                self._realtime_next_cli_poll = 0.0
                self.im.request_backfill()
                log.warning("WeLinkBot realtime transport unavailable; CLI history polling is active")
            was_connected = connected

            if connected and self.im.backfill_active and backfill_deadline and now >= backfill_deadline:
                pending = self.im.backfill_pending_count
                self.im.cancel_backfill()
                log.warning(
                    "WeLink bounded history backfill sweep reached max duration; stopping with pending=%d",
                    pending,
                )

            recovery_active = (not connected) or self.im.backfill_active
            self._realtime_receive_mode = "fallback" if not connected else ("backfill" if self.im.backfill_active else "realtime")
            self._realtime_backfill_until = backfill_deadline if self.im.backfill_active else 0.0
            cli_interval = self._realtime_fallback_interval if recovery_active else self._realtime_reconcile_interval
            if now >= next_cli_poll:
                try:
                    await self._ingest_dispatch_batch(await self.im.poll())
                except Exception:
                    log.exception("WeLink CLI fallback/reconciliation failed")
                # A reconnect sweep may have completed in this poll. If so, do
                # not schedule one more 3-second history poll; immediately fall
                # back to the low-frequency healthy reconciliation interval.
                after = time.monotonic()
                still_recovering = (not self.realtime.connected) or self.im.backfill_active
                next_cli_poll = after + (self._realtime_fallback_interval if still_recovering else self._realtime_reconcile_interval)
                self._realtime_next_cli_poll = next_cli_poll

    def stop(self):
        self.stop_event.set()

    def _spawn(self, coro, *, name: str):
        # Background work must not inherit a peer-Agent reply target from the
        # foreground request that happened to spawn it. Otherwise a later task
        # completion could be accidentally addressed back to the peer and start
        # an unintended protocol exchange.
        if hasattr(self, "_collaboration_reply_context"):
            ctx = contextvars.copy_context()
            ctx.run(self._collaboration_reply_context.set, None)
            task = asyncio.create_task(coro, name=name, context=ctx)
        else:
            task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _spawn_workflow(self, workflow_id: str, coro):
        current = self._workflow_runners.get(workflow_id)
        if current and not current.done():
            return current
        task = self._spawn(coro, name=f"workflow:{workflow_id}")
        self._workflow_runners[workflow_id] = task
        def _done(_):
            if self._workflow_runners.get(workflow_id) is task:
                self._workflow_runners.pop(workflow_id, None)
        task.add_done_callback(_done)
        return task

    async def _send_text(self, conversation_id: str, text: str, *, collaboration_wrap: bool = True) -> None:
        """Durably queue and send one logical reply in transport-safe parts.

        V1.10 keeps the conversation lock across the whole logical reply.  For
        WeLink, Markdown is split so every physical message fits the configured
        limit and contains at most one native code block.  All parts are
        persisted before the first send, preventing tail loss on process crash.
        """
        lock = self._send_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            peer_ctx = self._collaboration_reply_context.get() if collaboration_wrap else None
            peer_ctx = peer_ctx if peer_ctx and peer_ctx.get("conversation_id") == conversation_id else None
            if isinstance(self.im, WeLinkAdapter):
                prefix_budget = len(str(getattr(self.im, "reply_prefix", "") or ""))
                protocol_budget = 420 if peer_ctx else 0
                max_body_chars = max(256, self._welink_max_message_chars - prefix_budget - protocol_budget)
                parts = split_welink_markdown(text, max_chars=max_body_chars)
            else:
                parts = [str(text)]
            if peer_ctx:
                wrapped: list[str] = []
                for part in parts:
                    envelope = AgentEnvelope(
                        version=1,
                        from_id=self._collaboration_agent_id,
                        to_id=str(peer_ctx["peer_id"]),
                        message_type="response",
                        message_id=new_agent_message_id(),
                        reply_to=str(peer_ctx["message_id"]),
                        hop=int(peer_ctx.get("hop", 0)) + 1,
                    )
                    wrapped.append(encode_agent_message(envelope, part))
                parts = wrapped

            # Never let a newer reply overtake an older durable retry in the
            # same conversation.  New parts are still persisted immediately.
            older = self.store.query_one(
                "SELECT 1 FROM outbound_messages WHERE conversation_id=? AND state!='delivered' LIMIT 1",
                (conversation_id,),
            )
            row = self.store.query_one(
                "SELECT MAX(created_at_ms) AS ts FROM outbound_messages WHERE conversation_id=?",
                (conversation_id,),
            )
            last_ms = int(row["ts"] or 0) if row else 0
            base_ms = max(int(time.time() * 1000), last_ms + 1)
            queued: list[tuple[str, str, int]] = []
            with self.store.transaction() as conn:
                for index, part in enumerate(parts):
                    send_id = new_id("send")
                    created_ms = base_ms + index
                    conn.execute(
                        "INSERT INTO outbound_messages(send_id,conversation_id,text,state,created_at_ms) VALUES (?,?,?,'pending',?)",
                        (send_id, conversation_id, part, created_ms),
                    )
                    queued.append((send_id, part, created_ms))

            if older:
                return

            # Fresh batch: attempt parts in order.  If one attempt is ambiguous
            # or transient, leave it and all later parts pending; the ordered
            # outbox loop will resume from the first undelivered part.
            for send_id, part, created_ms in queued:
                delivered = await self._attempt_outbound(
                    send_id, conversation_id, part, created_ms,
                    verify_before_send=False, already_claimed=False,
                )
                if not delivered:
                    break

    async def _attempt_outbound(self, send_id: str, conversation_id: str, text: str, created_ms: int, *,
                                verify_before_send: bool, already_claimed: bool = False) -> bool:
        if not already_claimed:
            claimed = self.store.execute(
                "UPDATE outbound_messages SET state='sending' WHERE send_id=? AND state='pending'",
                (send_id,),
            )
            if claimed.rowcount != 1:
                # Another sender/retry loop already owns this send_id.
                return False
        try:
            if isinstance(self.im, WeLinkAdapter):
                await self.im.send_text(
                    conversation_id, text, created_at_ms=created_ms, verify_before_send=verify_before_send
                )
            else:
                await self.im.send_text(conversation_id, text)
        except (WeLinkSendAmbiguousError, WeLinkTransientError) as exc:
            row = self.store.query_one("SELECT attempts FROM outbound_messages WHERE send_id=?", (send_id,))
            attempts = int(row["attempts"] if row else 0) + 1
            delay = min(self._outbound_retry_max, self._outbound_retry_base * (2 ** min(attempts - 1, 6)))
            if "429" in str(exc) or "rate limit" in str(exc).lower():
                delay = max(delay, 30)
            self.store.execute(
                "UPDATE outbound_messages SET state='pending',attempts=?,last_error=?,next_attempt_at=unixepoch()+? WHERE send_id=?",
                (attempts, str(exc)[-1000:], delay, send_id),
            )
            log.warning("WeLink reply %s queued for retry in %ss: %s", send_id, delay, str(exc).splitlines()[-1][:240])
            return False
        except Exception as exc:
            row = self.store.query_one("SELECT attempts FROM outbound_messages WHERE send_id=?", (send_id,))
            attempts = int(row["attempts"] if row else 0) + 1
            self.store.execute(
                "UPDATE outbound_messages SET state='pending',attempts=?,last_error=?,next_attempt_at=unixepoch()+? WHERE send_id=?",
                (attempts, str(exc)[-1000:], self._outbound_retry_max, send_id),
            )
            log.exception("WeLink reply %s could not be delivered; left in durable outbox", send_id)
            return False
        self.store.execute(
            "UPDATE outbound_messages SET state='delivered',delivered_at=unixepoch(),last_error=NULL WHERE send_id=? AND state='sending'",
            (send_id,),
        )
        try:
            self.conversations.add_outgoing(conversation_id, text, send_id)
        except Exception:
            log.exception("failed to persist outgoing conversation message")
        return True

    async def _outbound_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                # Only the oldest undelivered row of each conversation may
                # retry.  This preserves order for V1.10 multi-part replies even
                # when an earlier send is temporarily ambiguous/rate-limited.
                rows = self.store.query_all(
                    "SELECT o.* FROM outbound_messages o "
                    "WHERE o.state='pending' AND o.next_attempt_at<=unixepoch() "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM outbound_messages p "
                    "  WHERE p.conversation_id=o.conversation_id "
                    "    AND p.state!='delivered' "
                    "    AND (p.created_at_ms<o.created_at_ms "
                    "         OR (p.created_at_ms=o.created_at_ms AND p.send_id<o.send_id))"
                    ") ORDER BY o.created_at_ms LIMIT 20"
                )
                for row in rows:
                    conversation_id = str(row["conversation_id"])
                    lock = self._send_locks.setdefault(conversation_id, asyncio.Lock())
                    async with lock:
                        await self._attempt_outbound(
                            str(row["send_id"]), conversation_id, str(row["text"]),
                            int(row["created_at_ms"]), verify_before_send=True,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbound retry loop failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    def _default_node(self, instruction: str = "") -> str | None:
        return self.nodes.choose(instruction, preferred=self.cfg.get("default_node"))

    # ------------------------------------------------------------------
    # V0.7 non-blocking IM dispatch
    # ------------------------------------------------------------------
    async def _ingest_im_message(self, msg: IncomingMessage) -> bool:
        # Ingestion is intentionally broader than reply/execution permission:
        # WorkBot may learn conversation-local context from messages it must not
        # answer or act on. Operators can still deny ingestion explicitly.
        ingest = self.ingestion_policy.inbound(msg)
        if not ingest.allowed:
            if self.ingestion_policy.log_denied:
                log.info("Skipping IM ingestion %s <%s>: %s", msg.conversation_id, msg.sender_id, ingest.reason)
            return False
        added = self.conversations.add_incoming(msg)
        if isinstance(self.im, WeLinkAdapter):
            # Even a duplicate history alias is useful cursor evidence. Advance
            # fallback cursors after durable-equivalent detection so reconnect
            # sweeps do not keep rediscovering the same Hook-delivered message.
            self.im.note_ingested_message(msg)
        if not added:
            log.info(
                "IM duplicate suppressed %s <%s> via %s id=%s",
                msg.conversation_id, msg.sender_id, msg.transport or "unknown", msg.external_message_id,
            )
            return False
        log.info("IM received %s <%s> via %s: %s", msg.conversation_id, msg.sender_id, msg.transport or "unknown", msg.content.strip())
        return True

    def _reply_allowed(self, msg: IncomingMessage) -> bool:
        return self.reply_policy.inbound(msg).allowed

    def _execution_allowed(self, msg: IncomingMessage) -> bool:
        decision = self.policy.inbound(msg)
        if decision.allowed:
            return True
        # V1.11.1: only a manually-sent local DM may use the inferred self
        # identity fallback. Peer DMs (from_self=false) never gain permission.
        if msg.conversation_kind == "user" and msg.from_self and len(self._operator_self_accounts) == 1:
            account = next(iter(self._operator_self_accounts))
            return account in self.policy.allow_senders
        return False

    @staticmethod
    def _alias_pattern(alias: str) -> re.Pattern[str] | None:
        """Match a bot alias only at the start of the human-readable text.

        Leading whitespace is allowed.  Transport-level non-text embeds (image,
        file, audio, video) are removed by the WeLink normalizer before this
        gate, so an image followed by ``bot ...`` still addresses WorkBot.
        Aliases appearing later in ordinary human text must never trigger a
        reply (for example ``没有bot也会了吗``).
        """
        alias = " ".join(str(alias or "").split())
        if not alias:
            return None
        # Normalize internal whitespace while preserving punctuation such as @.
        core = r"\s+".join(re.escape(x) for x in alias.split(" "))
        # Anchor to the first textual token.  The trailing ASCII guard prevents
        # `bot` from matching `botany`, `workbot-test`, or path/identifier
        # fragments while still allowing natural CJK adjacency (`bot你好`).
        next_guard = r"A-Za-z0-9_.\\/\-"
        return re.compile(rf"^\s*({core})(?![{next_guard}])", re.IGNORECASE)

    def _strip_bot_alias(self, text: str) -> tuple[bool, str]:
        value = str(text or "")
        for alias in sorted(self._bot_aliases, key=len, reverse=True):
            pat = self._alias_pattern(alias)
            if pat is None:
                continue
            match = pat.match(value)
            if not match:
                continue
            stripped = value[match.end():].strip()
            # Remove one common punctuation separator left by `bot: /status`.
            stripped = re.sub(r"^[\s,，:：;；]+", "", stripped).strip()
            return True, stripped
        return False, value.strip()

    @staticmethod
    def _is_known_slash_command(text: str) -> bool:
        """Return True only for WorkBot slash commands with valid basic syntax.

        WeLink embeds can begin with transport strings such as ``/:um_begin``
        and ordinary chat may contain arbitrary ``/foo`` text. A leading slash
        therefore bypasses strict bot-alias gating only when the whole text
        matches one of WorkBot's supported command forms.
        """
        value = str(text or "").strip()
        if not value.startswith("/"):
            return False
        return any(pat.fullmatch(value) is not None for pat in KNOWN_SLASH_COMMAND_PATTERNS)

    @staticmethod
    def _pending_natural_decision(text: str) -> str | None:
        value = str(text or "")
        if PENDING_CONFIRM_RE.fullmatch(value):
            return "confirm"
        if PENDING_CANCEL_RE.fullmatch(value):
            return "cancel"
        return None

    def _pending_sender_authorized(self, msg: IncomingMessage, pending: PendingAction) -> bool:
        """Bind a Pending Action decision to the operator who authorized it.

        New V1.12.2 proposals persist ``authorized_by`` in their payload.  For a
        legacy pending row left across an upgrade, recover ownership from the
        durable authorization/origin message when possible.  Never treat broad
        execution permission as permission to take over somebody else's prompt.
        """
        payload = pending.payload or {}
        owner = str(payload.get("authorized_by") or "").strip()
        if not owner:
            message_id = str(payload.get("authorization_message_id") or payload.get("origin_message_id") or "").strip()
            if message_id:
                row = self.store.query_one(
                    "SELECT sender_id FROM messages WHERE conversation_id=? AND external_message_id=? LIMIT 1",
                    (msg.conversation_id, message_id),
                )
                if row:
                    owner = str(row["sender_id"] or "").strip()
        # Legacy <=1.12.1 pending rows did not persist an owner. Preserve the
        # one-shot upgrade path for those rows; all V1.12.2-created pending
        # actions carry explicit authorization provenance.
        return True if not owner else owner == str(msg.sender_id)

    def _pending_natural_dispatch_message(self, msg: IncomingMessage) -> IncomingMessage | None:
        """Return msg only when a bare pending reply may bypass strict alias.

        This method deliberately does not execute/consume anything; it only
        opens the dispatch gate when a Pending Action exists.  Ownership is
        enforced atomically in ``_process_im_message`` immediately before the
        pending response is consumed.
        """
        if self._pending_natural_decision(msg.content) is None:
            return None
        if not self.conversations.get_pending_action(msg.conversation_id):
            return None
        return msg

    def _alias_dispatch_message(self, msg: IncomingMessage) -> IncomingMessage | None:
        matched, stripped = self._strip_bot_alias(msg.content)
        candidate = stripped if matched else str(msg.content or "").strip()
        # Any slash-looking first textual token is treated as command syntax, not
        # free-form chat. Unknown/invalid commands are store-only even in legacy
        # intent mode, which also provides a transport-safety fallback if a future
        # WeLink embed protocol starts with '/' and escapes media normalization.
        if candidate.startswith("/") and not self._is_known_slash_command(candidate):
            log.info(
                "IM unknown-slash store-only %s <%s> command=%s via %s",
                msg.conversation_id, msg.sender_id, candidate.split(None, 1)[0][:80], msg.transport or "unknown",
            )
            return None
        if self._require_bot_alias and not matched:
            if self._is_known_slash_command(candidate):
                # Slash control commands are an intentional compact UI surface.
                # Only the explicit WorkBot whitelist may bypass the alias gate.
                return replace(msg, content=candidate)
            log.info(
                "IM alias-gate store-only %s <%s> via %s",
                msg.conversation_id, msg.sender_id, msg.transport or "unknown",
            )
            return None
        # Alias stripping is part of strict V1.6 dispatch only. Legacy intent
        # mode keeps the original text because its group router still uses the
        # alias itself as a direct-address signal.
        if self._require_bot_alias and matched:
            return replace(msg, content=candidate or "你好")
        return msg

    def _cheap_group_intent_candidate(self, msg: IncomingMessage) -> bool:
        text = " ".join(msg.content.split()).lower()
        if not text:
            return False
        if any((" ".join(a.split())).lower() in text for a in self._bot_aliases if a):
            return True
        markers = ("?", "？", "请问", "帮我", "帮忙", "麻烦", "能否", "能不能", "可以帮", "查一下", "查询", "看一下", "分析一下", "总结一下", "执行", "创建任务")
        return any(x in text for x in markers)

    def _directed_to_bot(self, msg: IncomingMessage) -> bool:
        text = " ".join(msg.content.split())
        matched, _ = self._strip_bot_alias(msg.content)
        if self._require_bot_alias:
            return matched
        if self._is_known_slash_command(msg.content):
            return True
        if msg.conversation_kind == "user":
            return True
        if msg.is_at:
            return True
        if self._pending_natural_decision(text) and self.conversations.get_pending_action(msg.conversation_id):
            return True
        return matched


    @staticmethod
    def _is_execution_control_message(text: str) -> bool:
        value = text.strip()
        low = value.lower()
        prefixes = (
            "/approve ", "/deny ", "/approvals", "/cancel", "/retry", "/resume", "/continue", "/add", "/steer", "/stop-session", "/stop-sessions",
            "/remember ", "/forget ", "/task ", "/workflow ", "/test ", "/rag sync", "/rag reindex", "/rag embed",
            "/session set ", "/newsession", "/reset-session", "/summary refresh", "/summary-reset",
        )
        if any(low.startswith(x) for x in prefixes):
            return True
        return value.startswith(("批准 approval-", "拒绝 approval-", "取消任务", "停止任务", "继续任务", "恢复工作流", "刷新摘要", "重建摘要", "清空摘要"))

    @staticmethod
    def _is_operator_internal_command(text: str) -> bool:
        """Commands that expose WorkBot internals or mutate control-plane state.

        Reply Policy is deliberately broad read-only *work Q&A* permission. It
        does not grant visibility into WorkBot's private topology, session,
        memory store, task state, diagnostics, or approval queue. Those remain
        operator/execution-plane capabilities.
        """
        value = text.strip()
        low = value.lower()
        exact = {
            "/status", "/session", "/sessions", "/memories", "/nodes", "/welink", "/welink-tools", "/workspace", "/rag", "/approvals",
            "状态", "进度", "会话状态", "当前会话", "记忆列表", "节点", "节点状态",
            "welink状态", "welink工具", "待审批", "刷新摘要", "重建摘要", "清空摘要",
        }
        if value in exact or low in exact:
            return True
        prefixes = (
            "/memory ", "/workspace ", "/rag ", "/remember ", "/forget ", "/task ", "/workflow ", "/test ",
            "/approve ", "/deny ", "/cancel", "/retry", "/resume", "/continue", "/add", "/steer",
            "/session set ", "/newsession", "/reset-session", "/summary ", "/stop-session ", "/stop-sessions", "/agent ",
        )
        return any(low.startswith(x) for x in prefixes) or value.startswith((
            "批准 approval-", "拒绝 approval-", "取消任务", "停止任务", "继续任务", "调整任务", "追加任务", "恢复工作流",
        ))

    @staticmethod
    def _is_fast_control_message(text: str) -> bool:
        value = text.strip()
        low = value.lower()
        if value in {"状态", "进度", "/status", "/session", "/sessions", "会话状态", "当前会话", "/memories", "记忆列表", "/nodes", "节点", "节点状态", "/welink", "welink状态", "/welink-tools", "welink工具", "/workspace", "/rag"}:
            return True
        if low.startswith("/memory ") or low.startswith("/forget "):
            return True
        if low.startswith("/rag "):
            return True
        if low.startswith("/stop-session ") or low == "/stop-sessions" or low.startswith("/stop-sessions "):
            return True
        if low.startswith("/agent "):
            return True
        if low == "/approvals" or low.startswith("/approve ") or low.startswith("/deny ") or value.startswith("批准 approval-") or value.startswith("拒绝 approval-"):
            return True
        # Explicit-ID cancellation is safe to run while a Conversation Agent is
        # thinking. Bare /cancel remains ordered because it may mean "cancel the
        # pending proposal" rather than "cancel the latest running task".
        if low.startswith("/cancel ") and ("task-" in low or "wf-" in low):
            return True
        if (low.startswith("/steer ") or low.startswith("/add ")) and ("task-" in low or "wf-" in low):
            return True
        return False

    def _enqueue_conversation_message(self, msg: IncomingMessage) -> None:
        queue = self._conversation_queues.setdefault(msg.conversation_id, asyncio.Queue())
        queue.put_nowait(msg)
        worker = self._conversation_workers.get(msg.conversation_id)
        if not worker or worker.done():
            worker = self._spawn(self._conversation_worker(msg.conversation_id), name=f"im:{msg.conversation_id}")
            self._conversation_workers[msg.conversation_id] = worker

    def _dispatch_im_message(self, msg: IncomingMessage) -> None:
        reply_ok = self._reply_allowed(msg)
        exec_ok = self._execution_allowed(msg)
        # Manual outgoing DMs (WeLinkBot isMine=true) are valid operator
        # instructions too. WorkBot's own echoes are already suppressed by the
        # adapter's outbound fingerprint/prefix checks, so SELF messages can
        # safely proceed through the same strict alias/slash dispatch gate.
        if msg.media_only:
            # Pure image/file/audio/video events are useful Conversation context
            # but are not textual requests.  Never spend intent/CodeAgent
            # bandwidth explaining that WorkBot cannot view a media placeholder.
            log.info(
                "IM media-only store-only %s <%s> types=%s via %s",
                msg.conversation_id, msg.sender_id, ",".join(msg.media_types) or "media", msg.transport or "unknown",
            )
            return
        collab_recognized, collab_msg = self._route_collaboration_message(msg)
        if collab_recognized:
            dispatch_msg = collab_msg
        else:
            dispatch_msg = self._pending_natural_dispatch_message(msg) or self._alias_dispatch_message(msg)
        if dispatch_msg is None:
            return
        msg = dispatch_msg
        # Alias gating is dispatch-only: the original message is already in
        # Conversation Store. Only a real WorkBot interaction resets the local
        # knowledge idle window; ambient chatter does not.
        self._last_interactive_activity = time.monotonic()
        if not reply_ok and not exec_ok:
            # Stored for conversation context/memory, but never invokes Agent.
            return
        if self._is_fast_control_message(msg.content) and (reply_ok or exec_ok):
            self._spawn(self._process_im_message_safe(msg), name=f"im-fast:{msg.external_message_id}")
            return
        if self._require_bot_alias:
            # The alias was already proven and stripped by _alias_dispatch_message.
            # Do not run a second alias test on the stripped text.
            self._enqueue_conversation_message(msg)
            return
        if not self._intent_enabled or msg.conversation_kind == "user" or self._directed_to_bot(msg):
            self._enqueue_conversation_message(msg)
            return
        # Multi-person groups are ambient by default. Cheap lexical screening
        # avoids spending inference on pure chatter; important groups may still
        # batch all messages to preserve conversational cues.
        if msg.external_conversation_id not in self._important_groups and not self._cheap_group_intent_candidate(msg):
            return
        batch = self._ambient_batches.setdefault(msg.conversation_id, [])
        batch.append(msg)
        if len(batch) >= self._ambient_max_batch:
            task = self._ambient_flushers.pop(msg.conversation_id, None)
            if task and not task.done():
                task.cancel()
            self._spawn(self._flush_ambient_batch(msg.conversation_id), name=f"intent:{msg.conversation_id}:full")
        elif msg.conversation_id not in self._ambient_flushers or self._ambient_flushers[msg.conversation_id].done():
            delay = self._important_ambient_delay if msg.external_conversation_id in self._important_groups else self._ambient_delay
            task = self._spawn(self._delayed_ambient_flush(msg.conversation_id, delay), name=f"intent-delay:{msg.conversation_id}")
            self._ambient_flushers[msg.conversation_id] = task

    async def _delayed_ambient_flush(self, conversation_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._flush_ambient_batch(conversation_id)
        finally:
            current = self._ambient_flushers.get(conversation_id)
            if current is asyncio.current_task():
                self._ambient_flushers.pop(conversation_id, None)

    async def _flush_ambient_batch(self, conversation_id: str) -> None:
        batch = self._ambient_batches.pop(conversation_id, [])
        if not batch:
            return
        eligible = [m for m in batch if self._reply_allowed(m) or self._execution_allowed(m)]
        if not eligible:
            return
        stats = self.agents.scheduler_stats()
        if self._ambient_max_scheduler_waiting and stats["waiting"] >= self._ambient_max_scheduler_waiting:
            log.info("dropping ambient intent batch for %s because CodeAgent queue is busy: %s", conversation_id, stats)
            return
        try:
            selected = set(await self.agents.classify_group_intents(
                conversation_id,
                [{"external_message_id": m.external_message_id, "sender_id": m.sender_id, "content": m.content} for m in eligible],
                bot_aliases=self._bot_aliases,
            ))
        except Exception:
            log.exception("ambient group intent classification failed for %s", conversation_id)
            return
        for msg in eligible:
            if msg.external_message_id in selected:
                log.info("Ambient group intent selected message=%s conversation=%s", msg.external_message_id, conversation_id)
                self._enqueue_conversation_message(msg)


    async def _conversation_worker(self, conversation_id: str) -> None:
        queue = self._conversation_queues[conversation_id]
        while not self.stop_event.is_set():
            msg = await queue.get()
            try:
                await self._process_im_message_safe(msg)
            finally:
                queue.task_done()

    async def _process_im_message_safe(self, msg: IncomingMessage) -> None:
        token = None
        if msg.agent_peer_id and msg.agent_message_type == "request":
            token = self._collaboration_reply_context.set({
                "conversation_id": msg.conversation_id,
                "peer_id": msg.agent_peer_id,
                "message_id": msg.agent_message_id,
                "hop": int(msg.agent_hop or 0),
            })
        try:
            await self._process_im_message(msg)
            # Fast control commands may run concurrently with a foreground
            # conversation invocation. Do not let /sessions, /status, /approve,
            # etc. opportunistically launch a summary Agent beside that
            # foreground work; deferred maintenance will summarize later.
            if not self._is_fast_control_message(msg.content.strip()):
                self._maybe_schedule_summary(msg.conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("IM processing failed conversation=%s message=%s", msg.conversation_id, msg.external_message_id)
        finally:
            if token is not None:
                self._collaboration_reply_context.reset(token)

    # ------------------------------------------------------------------
    # V0.5 conversation/session + V0.6 memory maintenance
    # ------------------------------------------------------------------
    def _maybe_schedule_summary(self, conversation_id: str, *, every: int | None = None) -> None:
        threshold = self._summary_every if every is None else int(every)
        if threshold <= 0 or conversation_id in self._summary_inflight:
            return
        # Never start background summarization beside foreground/background
        # CodeAgent work already associated with the same Conversation. This
        # avoids confusing /sessions output and unnecessary contention.
        if self.agents.has_live_agent_for_conversation(conversation_id):
            return
        conv = self.conversations.get(conversation_id)
        if not conv:
            return
        count = self.conversations.message_count(conversation_id)
        last = int(conv["summary_message_count"] or 0)
        if count < self._summary_min or count - last < threshold:
            return
        self._summary_inflight.add(conversation_id)
        task = self._spawn(self._refresh_summary(conversation_id, count), name=f"summary:{conversation_id}")
        task.add_done_callback(lambda _: self._summary_inflight.discard(conversation_id))

    async def _refresh_summary(self, conversation_id: str, count: int) -> None:
        try:
            summary = await self.agents.summarize_conversation(conversation_id)
            if summary:
                self.conversations.set_summary(conversation_id, summary, count)
                # Conversation summaries are lossy model-generated caches, not
                # authoritative evidence. Do not promote them into durable
                # memory unless the operator explicitly opts into that risk.
                if bool(self.memory_cfg.get("allow_summary_auto_capture", False)) and self._auto_memory_source("summary"):
                    self._schedule_memory_capture(conversation_id, summary, source=f"summary:{conversation_id}", source_kind="summary")
        except AgentInvocationStopped as exc:
            log.info("conversation summarization stopped by operator conversation=%s: %s", conversation_id, exc)
        except Exception:
            log.exception("conversation summarization failed for %s", conversation_id)

    def _workspace_knowledge_window_open(self) -> bool:
        if not self.workspace_knowledge.enabled:
            return False
        hour = datetime.now().hour
        start, end = self._knowledge_night_start, self._knowledge_night_end
        night = start <= hour < end if start <= end else (hour >= start or hour < end)
        idle = (time.monotonic() - self._last_interactive_activity) >= self._knowledge_idle_grace
        return night or idle

    async def _maintenance_loop(self) -> None:
        """Low-priority conversation memory extraction and global promotion."""
        while not self.stop_event.is_set():
            try:
                if self.agents.scheduler.is_idle():
                    did = await self._maintenance_summary_once()
                    if not did and self.agents.scheduler.is_idle():
                        did = await self._maintenance_conversation_memory_once()
                    if not did and self.agents.scheduler.is_idle() and self._workspace_knowledge_window_open():
                        did = await self.workspace_knowledge.maintenance_once(self.agents)
                    if not did and self.agents.scheduler.is_idle() and self._workspace_knowledge_window_open():
                        # Sync canonical memory/workspace chunks first, then embed
                        # only a bounded batch so a large corpus cannot monopolize
                        # the maintenance scheduler.
                        did = await self.rag.maintenance_once()
                    if not did and self.agents.scheduler.is_idle():
                        await self._maintenance_global_promotion_once()
            except asyncio.CancelledError:
                raise
            except AgentInvocationStopped as exc:
                log.info("background maintenance CodeAgent stopped by operator: %s", exc)
            except Exception:
                log.exception("background maintenance failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self._maintenance_interval)
            except asyncio.TimeoutError:
                pass

    async def _maintenance_summary_once(self) -> bool:
        row = self.store.query_one(
            "SELECT conversation_id FROM conversations WHERE (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=conversations.conversation_id) - COALESCE(summary_message_count,0) >= ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (self._ambient_summary_every,),
        )
        if not row:
            return False
        cid = str(row["conversation_id"])
        count = self.conversations.message_count(cid)
        summary = await self.agents.summarize_conversation(cid)
        if summary:
            self.conversations.set_summary(cid, summary, count)
        return True

    async def _maintenance_conversation_memory_once(self) -> bool:
        row = self.store.query_one(
            "SELECT conversation_id, COALESCE(memory_message_count,0) AS memory_message_count "
            "FROM conversations WHERE (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=conversations.conversation_id) - COALESCE(memory_message_count,0) >= ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (self._memory_batch_messages,),
        )
        if not row:
            return False
        cid = str(row["conversation_id"])
        recent = self.conversations.recent_messages(cid, limit=min(120, self._memory_batch_messages * 2))
        material = "\n".join(f"{('USER' if m['direction']=='in' else ('SELF' if m['direction']=='self' else 'WORKBOT'))}[{m['sender_id']}]: {m['content']}" for m in recent)
        if not material.strip():
            return False
        await self._capture_memories(cid, material, source=f"conversation-batch:{cid}", source_kind="conversation")
        self.store.execute(
            "UPDATE conversations SET memory_message_count=(SELECT COUNT(*) FROM messages WHERE conversation_id=?), updated_at=unixepoch() WHERE conversation_id=?",
            (cid, cid),
        )
        return True

    async def _maintenance_global_promotion_once(self) -> None:
        candidates = self.memory.promotion_candidates(limit=20)
        if not candidates:
            return
        hour = datetime.now().hour
        night = self._promotion_night_start <= hour < self._promotion_night_end if self._promotion_night_start <= self._promotion_night_end else (hour >= self._promotion_night_start or hour < self._promotion_night_end)
        if len(candidates) < self._promotion_min_candidates and not night:
            return
        globals_ = self.memory.list(scopes=["global"], limit=20)
        promotions = await self.agents.verify_memory_promotions([dict(x) for x in candidates], [dict(x) for x in globals_])
        reviewed = [int(x["id"]) for x in candidates]
        self.memory.mark_promotion_reviewed(reviewed)
        for item in promotions:
            source_ids = item["candidate_ids"]
            mid = self.memory.remember(
                "global", item["title"], item["content"], source="promotion:" + ",".join(map(str, source_ids)),
                memory_kind=item["kind"], confidence=item["confidence"], evidence=item["evidence"], auto_generated=True,
            )
            self.memory.mark_promoted(source_ids, global_memory_id=mid)
            await asyncio.to_thread(self.rag.sync_memory)
            log.info("promoted conversation memories %s -> global memory #%s", source_ids, mid)

    def _auto_memory_source(self, source_kind: str) -> bool:
        if not bool(self.memory_cfg.get("auto_capture", True)):
            return False
        sources = self.memory_cfg.get("auto_capture_sources", ["workflow"])
        return source_kind in sources

    def _schedule_memory_capture(self, conversation_id: str, material: str, *, source: str, source_kind: str = "workflow") -> None:
        key = f"{conversation_id}:{source}"
        if key in self._memory_inflight:
            return
        self._memory_inflight.add(key)
        task = self._spawn(self._capture_memories(conversation_id, material, source=source, source_kind=source_kind), name=f"memory:{source}")
        task.add_done_callback(lambda _: self._memory_inflight.discard(key))

    async def _capture_memories(self, conversation_id: str, material: str, *, source: str, source_kind: str) -> None:
        try:
            # Conversation-derived material is untrusted by default and must
            # always land in conversation scope first. Global promotion is a
            # separate, low-priority fact-verification pass. This invariant is
            # enforced even if an older config still has auto_global=true.
            allow_global = bool(self.memory_cfg.get("auto_global", False)) and source_kind != "conversation"
            candidates = await self.agents.extract_memories(conversation_id, material, allow_global=allow_global)
            for item in candidates:
                self.memory.remember(
                    item["scope"], item["title"], item["content"], item["tags"], source=source,
                    memory_kind=item["memory_kind"], confidence=item["confidence"],
                    evidence=item.get("evidence"), auto_generated=True,
                )
            if candidates:
                await asyncio.to_thread(self.rag.sync_memory)
                log.info("captured %d durable memories from %s", len(candidates), source)
        except AgentInvocationStopped as exc:
            log.info("memory capture stopped by operator source=%s: %s", source, exc)
        except Exception:
            log.exception("memory capture failed from %s", source)

    # ------------------------------------------------------------------
    # V0.4 lifecycle commands
    # ------------------------------------------------------------------
    async def _handle_lifecycle_command(self, msg: IncomingMessage, text: str) -> bool:
        low = text.strip().lower()

        if low == "/agent discover":
            if not self._collaboration_enabled:
                await self._send_text(msg.conversation_id, "多 Agent 协作未启用。请配置 collaboration.enabled=true。")
                return True
            if not self._collaboration_discovery_enabled:
                await self._send_text(msg.conversation_id, "WorkBot 发现/握手已禁用。请配置 collaboration.discovery_enabled=true。")
                return True
            if msg.conversation_kind != "group":
                await self._send_text(msg.conversation_id, "/agent discover 仅用于允许协作的群聊。")
                return True
            if msg.external_conversation_id not in self._collaboration_groups:
                await self._send_text(msg.conversation_id, "当前群不在 collaboration.groups 中。")
                return True
            try:
                sent = await self.discover_workbots(msg.conversation_id)
                await self._send_text(msg.conversation_id, f"已发送一次 WorkBot 发现握手（hello {len(sent)} 条）。")
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"WorkBot 发现失败：{exc}")
            return True

        if low == "/agent peers":
            if not self._collaboration_enabled:
                await self._send_text(msg.conversation_id, "多 Agent 协作未启用。请配置 collaboration.enabled=true；启用后可用 /agent discover 手动发现 peers。")
                return True
            lines = [f"local: {self._collaboration_agent_id}"]
            peers = self.collaboration_peers_snapshot()
            if not peers:
                lines.append("peers: <none discovered yet>")
            else:
                lines.append("peers:")
                now = time.time()
                for cfg in peers:
                    accounts = ",".join(str(x) for x in (cfg.get("sender_accounts") or [])) or "<no sender binding>"
                    aliases = ",".join(str(x) for x in (cfg.get("aliases") or []))
                    source = str(cfg.get("source") or "unknown")
                    last_seen = float(cfg.get("last_seen") or 0.0)
                    seen = f"; seen={int(max(0, now-last_seen))}s ago" if last_seen else ""
                    suffix = f"; aliases={aliases}" if aliases else ""
                    lines.append(f"- {cfg['agent_id']}: source={source}; senders={accounts}{suffix}{seen}")
            await self._send_text(msg.conversation_id, "\n".join(lines))
            return True

        send_match = re.match(r"^/agent\s+send\s+([A-Za-z0-9_.:-]{1,128})\s+([\s\S]+)$", text.strip(), re.IGNORECASE)
        if send_match:
            if msg.conversation_kind != "group":
                await self._send_text(msg.conversation_id, "/agent send 仅用于群聊中的 WorkBot 协作。")
                return True
            peer_id, body = send_match.group(1), send_match.group(2).strip()
            if self._collaboration_groups and msg.external_conversation_id not in self._collaboration_groups:
                await self._send_text(msg.conversation_id, "当前群不在 collaboration.groups 中。")
                return True
            try:
                message_id = await self._send_collaboration_message(msg.conversation_id, peer_id, body)
                log.info("Peer Agent request sent to=%s id=%s by=%s", peer_id, message_id, msg.sender_id)
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"发送协作请求失败：{exc}")
            return True

        if low == "/agent status":
            state = self.agents.latest_agent_run(msg.conversation_id)
            if state is None:
                await self._send_text(msg.conversation_id, "当前会话还没有 CodeAgent run 记录。")
            else:
                now = time.time()
                last_age = "无输出" if state.last_output_at is None else f"{int(max(0, now - state.last_output_at))}s ago"
                tail = list(state.recent_output)[-5:]
                body = [
                    "CodeAgent status",
                    f"run_id: {state.run_id}",
                    f"status: {state.status}",
                    f"elapsed: {int(state.elapsed)}s",
                    f"conversation: {state.conversation_id or '-'}",
                    f"session: {state.session_id or '-'}",
                    f"pid: {state.pid or '-'}",
                    f"last output: {last_age}",
                ]
                if state.error:
                    body.append(f"error: {state.error}")
                if tail:
                    body.append("recent output:\n" + "\n".join(tail))
                await self._send_text(msg.conversation_id, "\n".join(body))
            return True

        if low.startswith("/agent tail"):
            parts = text.strip().split()
            lines = 20
            if len(parts) >= 3:
                try:
                    lines = max(1, min(100, int(parts[2])))
                except ValueError:
                    await self._send_text(msg.conversation_id, "用法：/agent tail [1-100]")
                    return True
            state, tail = self.agents.agent_run_tail(msg.conversation_id, lines)
            if state is None:
                await self._send_text(msg.conversation_id, "当前会话还没有 CodeAgent run 记录。")
            elif not tail:
                await self._send_text(msg.conversation_id, f"{state.run_id} 暂无 stdout/stderr 输出。")
            else:
                await self._send_text(msg.conversation_id, f"{state.run_id} 最近 {len(tail)} 行：\n" + "\n".join(tail))
            return True

        if low in {"/summary refresh", "/summary-refresh"} or text in {"刷新摘要", "重建摘要"}:
            count = self.conversations.message_count(msg.conversation_id)
            try:
                summary = await self.agents.summarize_conversation(msg.conversation_id, full_refresh=True)
                self.conversations.set_summary(msg.conversation_id, summary, count)
                await self._send_text(msg.conversation_id, f"已重建当前会话摘要：\n{summary[:1200] or '（空）'}")
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"重建会话摘要失败：{exc}")
            return True
        if low in {"/summary reset", "/summary-reset"} or text == "清空摘要":
            self.conversations.set_summary(msg.conversation_id, "", 0)
            await self._send_text(msg.conversation_id, "已清空当前会话摘要；后续达到摘要阈值时会重新生成。")
            return True

        # V0.8 action approval commands. These use a dedicated durable table
        # instead of Conversation.pending_action so a task can request approval
        # while the Conversation has other proposals/work in flight.
        if low == "/approvals" or text == "待审批":
            rows = self.approvals.pending(conversation_id=msg.conversation_id)
            if not rows:
                await self._send_text(msg.conversation_id, "当前会话没有待审批操作。")
            else:
                body = "\n".join(
                    f"- {r['approval_id']} [{r['action']}] {r['summary']}" +
                    (f"（{r['node']} / {r['task_id']}）" if r['node'] or r['task_id'] else "")
                    for r in rows
                )
                await self._send_text(msg.conversation_id, "当前待审批操作：\n" + body)
            return True

        approval_match = re.match(
            r"^(?:/(approve|deny)|(批准|拒绝))\s+(approval-[0-9a-fA-F]{6,32})(?:\s+(.+))?$",
            text.strip(), re.DOTALL | re.IGNORECASE,
        )
        if approval_match:
            approved = (approval_match.group(1) or approval_match.group(2)) in {"approve", "批准"}
            approval_id = approval_match.group(3)
            reason = (approval_match.group(4) or "").strip()
            try:
                approval_row = self.approvals.get(approval_id)
                if approval_row and str(approval_row["conversation_id"]) != msg.conversation_id:
                    raise PermissionError("approval must be decided from its originating conversation")
                result = self.approvals.decide(
                    approval_id, approved=approved, sender_id=msg.sender_id, reason=reason
                )
                if result.changed:
                    word = "已批准" if result.decision == "approved" else ("已拒绝" if result.decision == "denied" else f"当前状态为 {result.decision}")
                else:
                    word = f"已处理，当前状态为 {result.decision}，不会重复执行"
                await self._send_text(msg.conversation_id, f"[{approval_id}] {word}。" + (f"\n原因：{reason}" if reason else ""))
                if result.changed and result.decision == "approved" and approval_row:
                    details = self.store.loads(approval_row["details_json"], {})
                    if details.get("_resume_kind") == "conversation":
                        self._spawn(
                            self._resume_conversation_after_approval(dict(approval_row)),
                            name=f"approval-resume:{approval_id}",
                        )
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"处理审批 {approval_id} 失败：{exc}")
            return True

        if text in {"/session", "会话状态", "当前会话"}:
            conv = self.conversations.get(msg.conversation_id)
            session = conv["agent_session_id"] if conv else None
            turns = int(conv["session_turns"] or 0) if conv else 0
            summary = (conv["summary"] or "") if conv else ""
            await self._send_text(msg.conversation_id,
                f"CodeAgent session：{session or '尚未创建 session'}\n"
                f"session turns：{turns}\n"
                f"会话摘要：{summary[:800] or '尚未生成'}")
            return True

        session_set = re.match(r"^/session\s+set\s+([0-9a-fA-F-]{36})$", text.strip())
        if session_set:
            session_id = session_set.group(1)
            self.conversations.set_agent_session(msg.conversation_id, session_id)
            await self._send_text(msg.conversation_id, f"已将当前 Conversation 绑定到 CodeAgent session {session_id}。")
            return True

        if text in {"/newsession", "/reset-session", "新会话", "重置会话"}:
            self.conversations.clear_agent_session(msg.conversation_id)
            await self._send_text(msg.conversation_id, "已清除当前 Conversation 的 CodeAgent session；下一次 Agent 调用会创建新 session。")
            return True

        # Memory management commands.
        if text == "/memories" or text == "记忆列表":
            rows = self.memory.list(scopes=self.memory.relevant_scopes(msg.conversation_id), limit=12)
            if not rows:
                await self._send_text(msg.conversation_id, "当前没有可见记忆。")
            else:
                body = "\n".join(f"- #{r['id']} [{'auto/' if r['auto_generated'] else ''}{r['scope']}/{r['memory_kind']}] {r['title']}: {r['content'][:240]}" for r in rows)
                await self._send_text(msg.conversation_id, "当前相关记忆：\n" + body)
            return True
        if low in {"/forget auto-summary", "/forget summary-auto"}:
            n = self.memory.forget_auto_summary()
            if n:
                await asyncio.to_thread(self.rag.sync_memory)
            await self._send_text(msg.conversation_id, f"已删除 {n} 条由旧版 conversation summary 自动生成的记忆。")
            return True
        if low == "/forget auto":
            n = self.memory.forget_all_auto()
            if n:
                await asyncio.to_thread(self.rag.sync_memory)
            await self._send_text(msg.conversation_id, f"已删除 {n} 条自动生成的记忆；手工 /remember 记忆未删除。")
            return True
        if low.startswith("/forget "):
            raw = text.split(None, 1)[1].strip().lstrip("#")
            if raw.isdigit() and self.memory.forget(int(raw)):
                await asyncio.to_thread(self.rag.sync_memory)
                await self._send_text(msg.conversation_id, f"已删除记忆 #{raw}。")
            else:
                await self._send_text(msg.conversation_id, f"没有找到记忆 #{raw}。")
            return True

        task_match = TASK_ID_RE.search(text)
        wf_match = WF_ID_RE.search(text)

        cancel_intent = (
            low == "/cancel" or low.startswith("/cancel ") or text.startswith("取消任务") or
            text.startswith("停止任务") or text.startswith("停掉任务") or "停掉刚才任务" in text
        )
        if cancel_intent:
            if wf_match:
                await self._cancel_workflow(wf_match.group(0), notify=True)
                return True
            task_id = task_match.group(0) if task_match else None
            if not task_id:
                latest_wf = self.workflows.latest_for_conversation(msg.conversation_id, active_only=True)
                latest_task = self.tasks.latest_for_conversation(msg.conversation_id, active_only=True)
                # Prefer the latest active workflow because it may own child tasks.
                if latest_wf and (not latest_task or latest_wf["created_at"] >= latest_task["created_at"]):
                    await self._cancel_workflow(latest_wf["workflow_id"], notify=True)
                    return True
                task_id = latest_task["task_id"] if latest_task else None
            if not task_id:
                await self._send_text(msg.conversation_id, "当前没有可取消的运行中任务。")
                return True
            try:
                result = await self.tasks.cancel(task_id)
                if result.get("already_terminal"):
                    await self._send_text(msg.conversation_id, f"{task_id} 已是终态：{result.get('state')}。")
                # For an active task, the durable task.cancelled event is the
                # authoritative user notification; do not emit a duplicate ACK.
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"取消 {task_id} 失败：{exc}")
            return True

        retry_intent = low.startswith("/retry") or text.startswith("重试")
        if retry_intent:
            if wf_match:
                wf_id = wf_match.group(0)
                step_match = STEP_ID_RE.search(text)
                if not step_match:
                    failed = [s for s in self.workflows.steps(wf_id) if s["state"] in {"failed", "interrupted"}]
                    if len(failed) == 1:
                        step_id = failed[0]["step_id"]
                    else:
                        await self._send_text(msg.conversation_id, "请指定要重试的 workflow step，例如：/retry wf-xxx step-2")
                        return True
                else:
                    step_id = step_match.group(0)
                try:
                    await self._retry_workflow_step(wf_id, step_id)
                    await self._send_text(msg.conversation_id, f"已重新执行 {wf_id}/{step_id} 及其依赖该结果的后续步骤。")
                except Exception as exc:
                    await self._send_text(msg.conversation_id, f"重试工作流步骤失败：{exc}")
                return True
            task_id = task_match.group(0) if task_match else None
            if not task_id:
                row = self.tasks.latest_for_conversation(msg.conversation_id)
                task_id = row["task_id"] if row else None
            if not task_id:
                await self._send_text(msg.conversation_id, "当前会话没有可重试的任务。")
                return True
            try:
                new_id_ = await self.tasks.retry(task_id, conversation_id=msg.conversation_id,
                                                 origin_message_id=msg.external_message_id)
                await self._send_text(msg.conversation_id, f"已基于 {task_id} 创建重试任务 {new_id_}。")
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"重试任务失败：{exc}")
            return True

        latest_blocked = self.workflows.latest_for_conversation(msg.conversation_id, active_only=True)
        resume_wf_intent = (
            low.startswith("/resume") or text.startswith("继续工作流") or text.startswith("恢复工作流") or
            (text == "继续" and latest_blocked is not None and latest_blocked["state"] == "blocked")
        )
        if resume_wf_intent:
            wf_id = wf_match.group(0) if wf_match else None
            if not wf_id:
                wf = self.workflows.latest_for_conversation(msg.conversation_id, active_only=True)
                wf_id = wf["workflow_id"] if wf else None
            if not wf_id:
                await self._send_text(msg.conversation_id, "当前没有可恢复的工作流。")
                return True
            try:
                await self._resume_blocked_workflow(wf_id)
                await self._send_text(msg.conversation_id, f"已恢复工作流 {wf_id}。")
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"恢复工作流失败：{exc}")
            return True

        task_instruction_match = re.match(
            r"^/(steer|add)\s+(task-[0-9a-fA-F]{6,32})\s+(.+)$", text, re.DOTALL | re.IGNORECASE
        )
        wf_instruction_match = re.match(
            r"^/(steer|add)\s+(wf-[0-9a-fA-F]{6,32})\s+(step-[A-Za-z0-9_-]+)\s+(.+)$",
            text, re.DOTALL | re.IGNORECASE,
        )
        if task_instruction_match:
            mode = "steer" if task_instruction_match.group(1).lower() == "steer" else "append"
            task_id = task_instruction_match.group(2)
            instruction = task_instruction_match.group(3).strip()
            try:
                result = await self.tasks.add_instruction(task_id, instruction, mode=mode)
                if mode == "steer":
                    await self._send_text(
                        msg.conversation_id,
                        f"已调整 {task_id}：旧执行 turn 已停止，正在同一 CodeAgent session 中执行 instruction #{result.get('sequence')}（turn {result.get('turn')}）。",
                    )
                else:
                    await self._send_text(
                        msg.conversation_id,
                        f"已向 {task_id} 追加 instruction #{result.get('sequence')}；当前 turn 完成后将在同一 CodeAgent session 中继续执行。",
                    )
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"更新 {task_id} 指令失败：{exc}")
            return True
        if wf_instruction_match:
            mode = "steer" if wf_instruction_match.group(1).lower() == "steer" else "append"
            wf_id, step_id, instruction = wf_instruction_match.group(2), wf_instruction_match.group(3), wf_instruction_match.group(4).strip()
            try:
                result = await self._steer_workflow_step(wf_id, step_id, instruction, mode=mode)
                verb = "调整" if mode == "steer" else "追加"
                await self._send_text(msg.conversation_id, f"已{verb} {wf_id}/{step_id}：{result}")
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"更新 {wf_id}/{step_id} 指令失败：{exc}")
            return True

        continue_match = re.match(r"^(?:/continue|继续任务|继续)\s+(task-[0-9a-fA-F]{6,32})(?:\s+(.+))?$", text, re.DOTALL)
        if continue_match:
            task_id = continue_match.group(1)
            extra = (continue_match.group(2) or "继续完成上一任务，并根据当前上下文判断下一步。").strip()
            try:
                child = await self.tasks.continue_from(task_id, extra, conversation_id=msg.conversation_id,
                                                       origin_message_id=msg.external_message_id)
                await self._send_text(msg.conversation_id, f"已基于 {task_id} 创建继续任务 {child}。")
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"继续任务失败：{exc}")
            return True

        return False

    async def _enrich_rich_message(self, msg: IncomingMessage) -> IncomingMessage:
        has_quote_images = bool(msg.quote and msg.quote.get("image_paths"))
        if not msg.image_paths and not has_quote_images:
            return msg
        try:
            enriched = await self.image_context.enrich(msg)
            self.conversations.update_message_context(enriched)
            for item in enriched.image_context:
                if item.get("status") == "ok":
                    log.info("Image OCR success message=%s path=%s chars=%d", enriched.external_message_id, item.get("path"), len(str(item.get("text") or "")))
                else:
                    log.warning("Image enrichment degraded message=%s path=%s reason=%s", enriched.external_message_id, item.get("path"), item.get("reason") or item.get("status"))
            return enriched
        except Exception:
            log.exception("Image enrichment failed but message processing continues message=%s", msg.external_message_id)
            return msg

    # ------------------------------------------------------------------
    # IM dispatch
    # ------------------------------------------------------------------
    async def handle_im_message(self, msg: IncomingMessage):
        """Backwards-compatible direct handler used by tests and adapters.

        The production poll loop uses ingest+dispatch so CodeAgent latency does
        not block WeLink polling.
        """
        if not await self._ingest_im_message(msg):
            return
        msg = await self._enrich_rich_message(msg)
        collab_recognized, collab_msg = self._route_collaboration_message(msg)
        if collab_recognized:
            dispatch_msg = collab_msg
        else:
            dispatch_msg = self._pending_natural_dispatch_message(msg) or self._alias_dispatch_message(msg)
        if dispatch_msg is None or dispatch_msg.media_only:
            return
        token = None
        if dispatch_msg.agent_peer_id and dispatch_msg.agent_message_type == "request":
            token = self._collaboration_reply_context.set({
                "conversation_id": dispatch_msg.conversation_id,
                "peer_id": dispatch_msg.agent_peer_id,
                "message_id": dispatch_msg.agent_message_id,
                "hop": int(dispatch_msg.agent_hop or 0),
            })
        try:
            await self._process_im_message(dispatch_msg)
        finally:
            if token is not None:
                self._collaboration_reply_context.reset(token)
        if not self._is_fast_control_message(dispatch_msg.content.strip()):
            self._maybe_schedule_summary(msg.conversation_id)

    async def _process_im_message(self, msg: IncomingMessage):
        text = msg.content.strip()
        log.info("IM processing %s <%s>: %s", msg.conversation_id, msg.sender_id, text)
        execution_allowed = self._execution_allowed(msg)
        reply_allowed = self._reply_allowed(msg)
        pending_decision, pending = (None, None)
        natural_pending_decision = self._pending_natural_decision(text)
        current_pending = self.conversations.get_pending_action(msg.conversation_id) if natural_pending_decision else None
        if current_pending is not None and not self._pending_sender_authorized(msg, current_pending):
            # V1.12.4: natural confirmation words are common group chatter.
            # A non-authorized sender must neither consume the pending action nor
            # learn that somebody else currently has one. Store the message as
            # normal conversation context and silently stop control-plane handling.
            log.info(
                "Ignoring pending natural reply from non-authorized sender conversation=%s sender=%s",
                msg.conversation_id, msg.sender_id,
            )
            return
        if execution_allowed:
            pending_text = natural_pending_decision or text.strip()
            if natural_pending_decision == "confirm":
                pending_text = "确认"
            elif natural_pending_decision == "cancel":
                pending_text = "取消"
            pending_decision, pending = self.conversations.consume_pending_response(
                msg.conversation_id, pending_text, confirm_words=CONFIRM_WORDS, cancel_words=CANCEL_WORDS
            )
        # Reply Policy authorizes read-only work Q&A, not WorkBot's private
        # control plane. Keep slash diagnostics/state/memory/task commands
        # operator-only even when broad DM reply permission is enabled.
        if not execution_allowed and self._is_operator_internal_command(text):
            if not self._silent_unfulfillable and reply_allowed:
                await self._send_text(msg.conversation_id, "当前会话只有只读问答权限，不能访问 WorkBot 控制面或执行操作。")
            return
        if pending_decision == "cancel" and pending:
            await self._send_text(msg.conversation_id, "已取消待执行操作。")
            return
        if pending_decision == "confirm" and pending:
            await self._execute_pending(msg, pending, already_consumed=True)
            return

        if await self._handle_lifecycle_command(msg, text):
            return

        if text.startswith("/remember "):
            body = text[len("/remember "):].strip()
            scope = "global"
            if body.lower().startswith("conversation "):
                scope = f"conversation:{msg.conversation_id}"
                body = body[len("conversation "):].strip()
            elif body.lower().startswith("global "):
                body = body[len("global "):].strip()
            if "|" in body:
                title, content = [x.strip() for x in body.split("|", 1)]
            else:
                title, content = "WorkBot memory", body
            mid = self.memory.remember(scope, title or "WorkBot memory", content, source=msg.conversation_id)
            await asyncio.to_thread(self.rag.sync_memory)
            await self._send_text(msg.conversation_id, f"已写入记忆 #{mid}（{scope}）：{title or 'WorkBot memory'}")
            return

        if text.startswith("/memory "):
            query = text[len("/memory "):].strip()
            hits = await asyncio.to_thread(
                self.rag.search, query, conversation_id=msg.conversation_id, limit=6, source_types=["memory"]
            )
            if hits:
                body = []
                for h in hits:
                    mid = int((h.metadata or {}).get("memory_id") or str(h.source_key).removeprefix("memory:") or 0)
                    row = self.memory.get(mid) if mid else None
                    if row:
                        channels = []
                        if h.lexical_rank: channels.append(f"fts#{h.lexical_rank}")
                        if h.vector_rank: channels.append(f"vec#{h.vector_rank}")
                        body.append(f"- #{row['id']} [{row['scope']}; {'/'.join(channels) or 'hybrid'}] {row['title']}: {row['content']}")
                if body:
                    await self._send_text(msg.conversation_id, "相关记忆（Hybrid RAG）：\n" + "\n".join(body))
                    return
            rows = self.memory.search(query, limit=6, scopes=self.memory.relevant_scopes(msg.conversation_id))
            if not rows:
                await self._send_text(msg.conversation_id, "没有找到相关记忆。")
            else:
                body = "\n".join(f"- #{r['id']} [{r['scope']}] {r['title']}: {r['content']}" for r in rows)
                await self._send_text(msg.conversation_id, "相关记忆（legacy lexical fallback）：\n" + body)
            return

        if text in {"状态", "进度", "/status"}:
            await self._send_status(msg.conversation_id)
            return

        if text == "/sessions":
            await self._send_agent_sessions(msg.conversation_id)
            return

        if text.startswith("/stop-session "):
            invocation_id = text[len("/stop-session "):].strip()
            stopped, gone = await self.agents.stop_agent_session_and_wait(invocation_id, timeout=2.0)
            if not stopped:
                await self._send_text(msg.conversation_id, f"没有找到 active/stopping/waiting CodeAgent 调用 {invocation_id}。可用 /sessions 查看当前运行态。")
            else:
                purpose = stopped.get("purpose") or "agent"
                target_conv = str(stopped.get("conversation_id") or "")
                remaining = self.agents.agent_sessions_for_conversation(target_conv) if target_conv else []
                other = [r for r in remaining if r.get("invocation_id") != invocation_id]
                suffix = ""
                if other:
                    brief = ", ".join(f"{r.get('invocation_id')}[{r.get('purpose')}]" for r in other[:3])
                    suffix = f"\n注意：同一 Conversation 另有独立调用仍在运行：{brief}"
                if gone:
                    await self._send_text(msg.conversation_id, f"已停止 {invocation_id}（{purpose}）。仅终止该次 CodeAgent 调用，不清除持久 conversation session。" + suffix)
                elif stopped.get("already_stopping"):
                    await self._send_text(msg.conversation_id, f"{invocation_id} 已在停止中（{purpose}）；目标仍在进行 OS 进程清理，可用 /sessions 查看。" + suffix)
                else:
                    await self._send_text(msg.conversation_id, f"已请求停止 {invocation_id}（{purpose}）；2 秒内尚未完成 OS 进程清理，当前状态为 stopping。" + suffix)
            return

        if text == "/stop-sessions" or text == "/stop-sessions all":
            if text != "/stop-sessions all":
                await self._send_text(msg.conversation_id, "批量停止需要显式确认：/stop-sessions all")
                return
            stopped = self.agents.stop_all_agent_sessions()
            await self._send_text(msg.conversation_id, f"已请求停止 {len(stopped)} 个 active/waiting CodeAgent 调用。")
            return

        if text in {"/rag", "/rag status"}:
            d = self.rag.status()
            await self._send_text(msg.conversation_id,
                "Hybrid RAG：\n"
                f"- enabled/passive/active: {d['enabled']}/{d['passive_enabled']}/{d['active_enabled']}\n"
                f"- corpus documents/chunks: {d['documents']}/{d['chunks']}\n"
                f"- FTS5: {d['fts']}\n"
                f"- embedding: {d['embedding_provider']}\n"
                f"- dimension: {d['configured_dimension']}\n"
                f"- chunker: {d['chunker_version']}\n"
                f"- vector backend/index: {d['vector_backend']} / {d['vector_index'] or '未初始化'}\n"
                f"- embedded/pending/coverage: {d['embedded_chunks']}/{d['pending_chunks']}/{d['coverage_percent']}%\n"
                f"- embedding model batch/device/cpu_threads: {d['embedding_batch_size']}/{d['embedding_device']}/{d['embedding_cpu_threads']}\n"
                f"- memory retrieval scope: {d['memory_scope']}\n"
                f"- dependencies: sentence-transformers={d['sentence_transformers_installed']} sqlite-vec={d['sqlite_vec_installed']}\n"
                f"- sqlite-vec runtime: {d['sqlite_vec_available']} {d['sqlite_vec_version'] or ''}\n"
                f"- reranker: {d['reranker']}\n"
                f"- embed job: {json.dumps(d['embed_job'], ensure_ascii=False)}\n"
                f"- sync job: {json.dumps(d['sync_job'], ensure_ascii=False)}\n"
                f"- last embedding error: {d['last_embedding_error'] or '无'}\n"
                f"- last reranker error: {d['last_reranker_error'] or '无'}\n"
                "- commands: /rag sync [status|stop] | /rag embed [N|all|status|stop] | /rag search <query> | /rag reindex")
            return
        if text == "/rag sync":
            result = await self.rag.start_sync_job()
            await self._send_text(msg.conversation_id, "RAG corpus 后台同步任务已启动：\n" + json.dumps(result, ensure_ascii=False))
            return
        if text == "/rag sync status":
            await self._send_text(msg.conversation_id, "RAG sync 状态：\n" + json.dumps(self.rag.sync_job_status(), ensure_ascii=False))
            return
        if text == "/rag sync stop":
            await self._send_text(msg.conversation_id, "RAG sync 已请求停止：\n" + json.dumps(self.rag.stop_sync_job(), ensure_ascii=False))
            return
        if text.startswith("/rag embed"):
            arg = text[len("/rag embed"):].strip().lower()
            if arg == "status":
                await self._send_text(msg.conversation_id, "RAG embedding 状态：\n" + json.dumps(self.rag.embed_job_status(), ensure_ascii=False))
                return
            if arg == "stop":
                await self._send_text(msg.conversation_id, "RAG embedding 已请求停止：\n" + json.dumps(self.rag.stop_embed_job(), ensure_ascii=False))
                return
            all_chunks = arg == "all"
            limit = None if all_chunks else (int(arg) if arg.isdigit() else int((self.rag.cfg.get("index") or {}).get("batch_chunks", 24)))
            result = await self.rag.start_embed_job(limit=limit, all_chunks=all_chunks)
            await self._send_text(msg.conversation_id, "RAG embedding 后台任务已启动：\n" + json.dumps(result, ensure_ascii=False))
            return
        if text == "/rag reindex":
            try:
                result = await asyncio.to_thread(self.rag.force_reindex)
                await self._send_text(msg.conversation_id,
                    "已清空当前 RAG vector index 的 embedding 标记/向量；后续 maintenance 或 /rag embed 会按 content hash 重新构建。\n"
                    + json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                await self._send_text(msg.conversation_id, f"RAG reindex 失败：{exc}")
            return
        if text.startswith("/rag search "):
            query = text[len("/rag search "):].strip()
            hits = await asyncio.to_thread(self.rag.search, query, conversation_id=msg.conversation_id, limit=10)
            if not hits:
                await self._send_text(msg.conversation_id, "Hybrid RAG 没有找到相关证据。可先运行 /rag sync，并在安装 embedding 依赖后运行 /rag embed。")
            else:
                await self._send_text(msg.conversation_id, "Hybrid RAG 命中：\n" + self.rag.format_context(hits, max_chars=12000))
            return

        if text == "/workspace":
            d = self.workspace_knowledge.status()
            remote = d.get("remote_roots") or {}
            remote_text = "; ".join(f"{node}: {', '.join(roots)}" for node, roots in remote.items()) or "无"
            source_text = "; ".join(
                f"{name}={info.get('projects',0)}/{info.get('files',0)}/{info.get('chunks',0)}"
                + (f" ({'online' if info.get('online') else 'offline'})" if name != 'local' else "")
                for name, info in (d.get("sources") or {}).items()
            )
            await self._send_text(msg.conversation_id,
                "Workspace knowledge：\n"
                f"- enabled: {d['enabled']}\n"
                f"- local roots: {', '.join(d['roots']) or '无'}\n"
                f"- remote roots: {remote_text}\n"
                f"- indexed projects/files/chunks: {d['projects']}/{d['files']}/{d['chunks']}\n"
                f"- by source: {source_text or '无'}\n"
                f"- pending project analysis: {d.get('pending_analysis', 0)}\n"
                f"- FTS5: {d['fts']}\n"
                f"- idle grace/night: {self._knowledge_idle_grace}s / {self._knowledge_night_start}:00-{self._knowledge_night_end}:00\n"
                f"- scheduler: {self.agents.scheduler_stats()}")
            return

        if text.strip().lower() == "/workspace rescan":
            self.workspace_knowledge.force_rescan()
            await self._send_text(msg.conversation_id, "已标记 workspace knowledge 全量重新发现/增量扫描；将在空闲或夜间 maintenance 周期执行。")
            return

        if text.startswith("/workspace "):
            query = text[len("/workspace "):].strip()
            rag_hits = await asyncio.to_thread(
                self.rag.search, query, conversation_id=msg.conversation_id, limit=8, source_types=["workspace", "code"]
            )
            if rag_hits:
                await self._send_text(msg.conversation_id, "Workspace Hybrid RAG 命中：\n" + self.rag.format_context(rag_hits, max_chars=10000))
                return
            hits = self.workspace_knowledge.search(query, limit=8)
            if not hits:
                await self._send_text(msg.conversation_id, "Workspace index 中没有找到相关文件或项目。")
            else:
                body = "\n".join(f"- [{h['type']}] {h['path']}: {h['excerpt'][:300]}" for h in hits)
                await self._send_text(msg.conversation_id, "Workspace index 命中（legacy lexical fallback）：\n" + body)
            return

        if text in {"/welink-tools", "welink工具"}:
            cfg = self.cfg.get("welink_tools", {})
            body = """WeLink Tool Gateway：
- managed gateway: {enabled}
- CodeAgent skill: welink-cli-tool（用于命令语法）
- Reply Policy 可用：IM/联系人/会议/云空间/邮件/日历的只读查询
- Execution + Approval：IM发送/群变更、邮件发送或移动、会议/议题写入、日历创建、云空间写入/分享、联系人特别关注变更
- auth login/logout/config mutation：不允许 Agent 自动执行
- Agent IM history：共享 18/20 次每分钟预算
- Agent OneBox：每接口至少 1 秒间隔
- Agent IM send：批准后单次执行，并按 30 秒建议间隔限速
- approval token：一次性，只允许一个匹配的 WeLink write CLI 调用
- gateway trace: {trace}""".format(enabled=bool(cfg.get("managed_gateway", True)), trace=bool(cfg.get("gateway_trace", False)))
            await self._send_text(msg.conversation_id, body)
            return

        if text in {"/welink", "welink状态"}:
            if isinstance(self.im, WeLinkAdapter):
                d = self.im.diagnostics()
                err = d.get("last_error") or "无"
                body = "\n".join([
                    "WeLink CLI 状态：",
                    f"- cli: {d.get('cli')}",
                    f"- trace: {d.get('cli_trace')}",
                    f"- inflight: {d.get('inflight')}",
                    f"- calls/failures/transient: {d.get('calls')}/{d.get('failures')}/{d.get('transient_failures')}",
                    f"- last op: {d.get('last_op') or '无'}",
                    f"- last rc: {d.get('last_rc')}",
                    f"- last wait/duration: {d.get('last_wait_seconds')}s / {d.get('last_duration_seconds')}s",
                    f"- poll backoff remaining: {d.get('poll_backoff_remaining_seconds')}s",
                    f"- history pacing remaining: {d.get('history_pacing_remaining_seconds')}s",
                    f"- rate-limit cooldown/hits: {d.get('rate_limit_remaining_seconds')}s / {d.get('rate_limit_hits')}",
                    f"- history min interval: {d.get('history_min_interval_seconds')}s",
                    f"- history budget: {d.get('history_window_used')}/{d.get('history_budget_per_minute')} in 60s (documented limit {d.get('history_limit_per_minute')}/min)",
                    f"- history budget reset: {d.get('history_budget_reset_seconds')}s",
                    f"- last error: {err}",
                    f"- discovery: every {d.get('discovery_interval_seconds')}s, next in {d.get('discovery_next_seconds')}s",
                    f"- recent focus/reply boost: {d.get('recent_focus_count')} conversations / {d.get('reply_boost_seconds')}s",
                    f"- discovered conversations: {d.get('discovered_conversations')} ({d.get('discovery_mode')})",
                    f"- rich message image/OCR: {self.image_context.status().get('enabled')} / {self.image_context.status().get('ocr_enabled')} ({self.image_context.status().get('provider')})",
                    *([
                        f"- receive transport: WeLinkBot WebSocket primary ({self._realtime_receive_mode})",
                        f"- websocket connected/authenticated: {self.realtime.diagnostics().get('connected')} / {self.realtime.diagnostics().get('authenticated')}",
                        f"- websocket connections/reconnects: {self.realtime.diagnostics().get('connections')} / {self.realtime.diagnostics().get('reconnects')}",
                        f"- websocket messages/queue/dropped: {self.realtime.diagnostics().get('received_messages')} / {self.realtime.diagnostics().get('queue_size')} / {self.realtime.diagnostics().get('dropped_messages')}",
                        f"- websocket last event/message: {self.realtime.diagnostics().get('last_event_seconds_ago')}s / {self.realtime.diagnostics().get('last_message_seconds_ago')}s ago",
                        f"- CLI fallback/reconcile next: {round(max(0.0, self._realtime_next_cli_poll - time.monotonic()), 1)}s",
                        f"- reconnect backfill: active={self.im.diagnostics().get('backfill_active')} pending={self.im.diagnostics().get('backfill_pending')} deadline={round(max(0.0, self._realtime_backfill_until - time.monotonic()), 1)}s",
                        f"- websocket last error: {self.realtime.diagnostics().get('last_error') or '无'}",
                    ] if self.realtime is not None else ["- receive transport: welink-cli polling"]),
                    f"- CodeAgent scheduler: {self.agents.scheduler_stats()}",
                ])
                await self._send_text(msg.conversation_id, body)
            else:
                await self._send_text(msg.conversation_id, "当前 IM adapter 不是 WeLink。")
            return

        if text in {"/nodes", "节点", "节点状态"}:
            lines = []
            for n in self.nodes.all():
                state = "online" if n.online else "offline"
                caps = ", ".join(n.capabilities) if n.capabilities else "generic-codeagent"
                lines.append(f"- {n.name}: {state}，负载 {n.active_tasks}/{n.max_concurrent}，能力：{caps}；{n.description}")
            await self._send_text(msg.conversation_id, "节点状态：\n" + ("\n".join(lines) if lines else "未配置远端节点。"))
            return

        # Commands that mutate memory/task/workflow state require execution permission.
        if (text.startswith("/remember ") or text.startswith("/task ") or text.startswith("/workflow ") or text.startswith("/test ")) and not execution_allowed:
            if not self._silent_unfulfillable and reply_allowed:
                await self._send_text(msg.conversation_id, "当前会话只有只读问答权限，不能创建或修改执行任务。")
            return

        # V1.12 quote-aware execution. A current operator can quote a prior
        # executable request and say "处理这个" without restating it. The quote
        # supplies provenance/context; CURRENT sender permission supplies the
        # authorization. Informational quote requests still fall through to Agent.
        if await self._handle_quoted_execution_request(
            msg, text, execution_allowed=execution_allowed, reply_allowed=reply_allowed
        ):
            return

        # V1.11.3 delegated execution: an operator may explicitly take over a
        # previously stored request from another participant. Resolve the
        # durable source message first, then run normal deterministic routing
        # under the CURRENT operator's permission.
        if await self._handle_delegated_execution_request(
            msg, text, execution_allowed=execution_allowed, reply_allowed=reply_allowed
        ):
            return

        configured_nodes = tuple(self.cfg.get("nodes", {}).keys())
        default_node = self._default_node(text)
        scope = resolve_execution_scope(text, self.nodes.configs)
        if scope.targets:
            log.info(
                "execution scope resolved source=%s targets=%s execution=%s fresh=%s",
                scope.source, ",".join(scope.targets), scope.requires_execution, scope.fresh_execution,
            )

        if text.startswith("/workflow "):
            instruction = text[len("/workflow "):].strip()
            await self._propose_workflow(msg, instruction, scope=resolve_execution_scope(instruction, self.nodes.configs))
            return
        if text.startswith("/task "):
            body = text[len("/task "):].strip()
            task_scope = resolve_execution_scope(body, self.nodes.configs)
            if task_scope.multi_target and task_scope.requires_execution:
                await self._propose_workflow(msg, body, scope=task_scope)
                return
            first, sep, rest = body.partition(" ")
            if first in configured_nodes and sep:
                node, instruction = first, rest.strip()
            else:
                node, instruction = default_node, body
            await self._propose_remote_task(msg, instruction, node=node)
            return
        if text.startswith("/test "):
            instruction = text[len("/test "):].strip() or "执行用户指定的任务"
            await self._propose_remote_task(msg, instruction, node=default_node)
            return
        # V1.1 deterministic scope routing: once the framework can prove that
        # execution spans multiple targets, this request cannot fall through to
        # a conversational Agent or be collapsed into one remote task.
        if scope.multi_target and scope.requires_execution:
            if not execution_allowed:
                if reply_allowed:
                    ro = await self.agents.answer_read_only(msg.conversation_id, text)
                    if ro["can_reply"] and ro["confidence"] >= 0.65 and ro["answer"]:
                        await self._send_text(msg.conversation_id, ro["answer"])
                return
            await self._propose_workflow(msg, text, scope=scope)
            return
        if looks_like_mixed_workflow(text, configured_nodes):
            if execution_allowed:
                await self._propose_workflow(msg, text)
            return
        if is_explicit_remote_action(text, configured_nodes):
            if execution_allowed:
                node = configured_node_in_text(text, configured_nodes)
                await self._propose_remote_task(msg, text, node=node)
            return

        agent_text = self.conversations.format_message_for_agent(msg, content=text)
        try:
            if not execution_allowed:
                if not reply_allowed:
                    return
                ro = await self.agents.answer_read_only(msg.conversation_id, agent_text)
                if ro["can_reply"] and ro["confidence"] >= 0.65 and ro["answer"]:
                    await self._send_text(msg.conversation_id, ro["answer"])
                elif not self._silent_unfulfillable:
                    await self._send_text(msg.conversation_id, "这个问题目前无法由 WorkBot 在只读权限下可靠完成。")
                return
            result = await self.agents.answer(msg.conversation_id, agent_text)
            self._agent_failure_notifications.pop(msg.conversation_id, None)
            if result.approval:
                if result.text:
                    await self._send_text(msg.conversation_id, result.text)
                await self._handle_conversation_agent_approval(msg.conversation_id, result.approval)
                return
            if result.action:
                action_type = str(result.action.get("type") or "")
                if action_type == "remote_task":
                    requested_node = str(result.action.get("node") or "").strip()
                    instruction = str(result.action.get("instruction") or text).strip()
                    action_scope = resolve_execution_scope(instruction, self.nodes.configs)
                    if action_scope.multi_target and action_scope.requires_execution:
                        if result.text:
                            await self._send_text(msg.conversation_id, result.text)
                        log.warning("overriding reasoning-agent remote_task with workflow for multi-target scope=%s", action_scope.targets)
                        await self._propose_workflow(msg, instruction, recommendation=True, scope=action_scope)
                        return
                    node = requested_node if requested_node in configured_nodes else self.nodes.choose(
                        instruction, preferred=default_node
                    )
                    if result.text:
                        await self._send_text(msg.conversation_id, result.text)
                    await self._propose_remote_task(msg, instruction, node=node, recommendation=True)
                    return
                if action_type == "peer_request":
                    peer_id = str(result.action.get("peer") or "").strip()
                    instruction = str(result.action.get("instruction") or text).strip()
                    if result.text:
                        await self._send_text(msg.conversation_id, result.text)
                    if not self._collaboration_enabled:
                        await self._send_text(msg.conversation_id, "当前未启用多 Agent 协作，无法向 peer WorkBot 发起请求。")
                        return
                    if self._collaboration_peer_config(peer_id) is None:
                        await self._send_text(msg.conversation_id, f"尚未发现 peer Agent：{peer_id or '<empty>'}。请先在协作群运行 /agent discover，再用 /agent peers 查看。")
                        return
                    if not instruction:
                        await self._send_text(msg.conversation_id, "peer Agent 协作请求为空，未发送。")
                        return
                    prompt = f"建议向 peer Agent {peer_id} 发送协作请求：\n{preview(instruction)}\n是否发送？"
                    pending = PendingAction(
                        action_type="agent.peer_request",
                        payload={
                            "peer_id": peer_id,
                            "instruction": instruction,
                            "requested_by": msg.sender_id,
                            "authorized_by": msg.sender_id,
                            "authorization_message_id": msg.external_message_id,
                            "origin_message_id": msg.external_message_id,
                        },
                        prompt=prompt,
                    )
                    self.conversations.set_pending_action(msg.conversation_id, pending)
                    await self._send_text(msg.conversation_id, prompt)
                    return
                if action_type in {"task_steer", "task_append"}:
                    mode = "steer" if action_type == "task_steer" else "append"
                    task_id = str(result.action.get("task_id") or "").strip()
                    instruction = str(result.action.get("instruction") or text).strip()
                    if not task_id:
                        active = self.tasks.active_for_conversation(msg.conversation_id)
                        if len(active) == 1:
                            task_id = str(active[0]["task_id"])
                    task_row = self.tasks.get(task_id) if task_id else None
                    if not task_row or task_row["origin_conversation_id"] != msg.conversation_id or task_row["state"] not in {"created", "running"}:
                        if result.text:
                            await self._send_text(msg.conversation_id, result.text)
                        await self._send_text(msg.conversation_id, "没有找到唯一可调整的当前任务；请使用 /steer task-... 明确指定。")
                        return
                    if result.text:
                        await self._send_text(msg.conversation_id, result.text)
                    verb = "调整" if mode == "steer" else "追加"
                    prompt = f"将{verb}正在运行的 {task_id}。\n新指令：{preview(instruction)}\n是否执行？"
                    pending = PendingAction(
                        action_type=f"task.{mode}",
                        payload={"task_id": task_id, "instruction": instruction,
                                 "authorized_by": msg.sender_id, "authorization_message_id": msg.external_message_id},
                        prompt=prompt,
                    )
                    self.conversations.set_pending_action(msg.conversation_id, pending)
                    await self._send_text(msg.conversation_id, prompt)
                    return
                if action_type == "workflow":
                    instruction = str(result.action.get("instruction") or text).strip()
                    if result.text:
                        await self._send_text(msg.conversation_id, result.text)
                    await self._propose_workflow(msg, instruction, recommendation=True, scope=resolve_execution_scope(instruction, self.nodes.configs))
                    return
            answer = result.text.strip() or "已处理，但 Agent 没有返回文本结果。"
            await self._send_text(msg.conversation_id, answer)
        except AgentInvocationStopped as exc:
            # /stop-session is an operator control action, not an Agent failure.
            # The control conversation already received the stop acknowledgement;
            # keep the cancelled originating request silent.
            self._agent_failure_notifications.pop(msg.conversation_id, None)
            log.info("CodeAgent invocation stopped by operator conversation=%s: %s", msg.conversation_id, exc)
            return
        except Exception as exc:
            log.exception("codeagent failed")
            signature = f"{type(exc).__name__}:{exc}"
            now = time.monotonic()
            previous = self._agent_failure_notifications.get(msg.conversation_id)
            if previous and previous[0] == signature and now - previous[1] < self._agent_failure_cooldown:
                log.warning("Suppressing repeated CodeAgent failure notification for %s", msg.conversation_id)
                return
            self._agent_failure_notifications[msg.conversation_id] = (signature, now)
            await self._send_text(msg.conversation_id,
                f"CodeAgent 调用失败：{exc}\n你仍可使用 /task <任务描述> 验证远端任务链路。")

    async def _handle_conversation_agent_approval(self, conversation_id: str, approval: dict) -> None:
        action = str(approval.get("action") or "").strip().lower()
        summary = str(approval.get("summary") or action or "Windows CodeAgent action").strip()
        decision = self.policy.evaluate_action(action)
        if decision.decision == "deny":
            await self._send_text(conversation_id, f"Windows 操作被策略拒绝：{action}\n说明：{summary}\n原因：{decision.reason}")
            return
        if decision.decision == "allow":
            token = self._create_welink_approval_token(action)
            result = await self.agents.continue_after_approval(
                conversation_id, action=action, summary=summary, context="WorkBot policy auto-approved this action.",
                approval_token=token,
            )
            if result.approval:
                if result.text:
                    await self._send_text(conversation_id, result.text)
                await self._handle_conversation_agent_approval(conversation_id, result.approval)
            else:
                await self._send_text(conversation_id, (result.text.strip() or "已完成。"))
            return
        aid = self.approvals.create(
            conversation_id=conversation_id, node="office-pc", task_id=None, action=action, summary=summary,
            details={"agent_details": approval.get("details") or {}, "_resume_kind": "conversation"},
            requested_by="windows-codeagent",
        )
        self.approvals.mark_resume_pending(aid)
        await self._send_text(
            conversation_id,
            f"[{aid}] Windows CodeAgent 请求批准操作\n动作：{action}\n说明：{summary}\n"
            f"批准：/approve {aid}\n拒绝：/deny {aid} [原因]",
        )

    async def _resume_conversation_after_approval(self, approval_row: dict) -> None:
        conversation_id = str(approval_row.get("conversation_id") or "")
        approval_id = str(approval_row.get("approval_id") or "")
        if not conversation_id or not approval_id:
            return
        if not self.approvals.claim_resume(approval_id):
            log.warning("duplicate approval resume suppressed approval=%s", approval_id)
            return
        try:
            approved_action = str(approval_row.get("action") or "")
            token = self._create_welink_approval_token(approved_action)
            resume_started = time.monotonic()
            log.info("approval resume started approval=%s action=%s scheduler=%s", approval_row.get("approval_id"), approved_action, self.agents.scheduler_stats())
            result = await self.agents.continue_after_approval(
                conversation_id, action=approved_action,
                summary=str(approval_row.get("summary") or ""),
                context=f"approval_id={approval_row.get('approval_id')}",
                approval_token=token,
            )
            log.info("approval resume finished approval=%s action=%s duration=%.3fs", approval_row.get("approval_id"), approved_action, time.monotonic() - resume_started)
            if result.approval:
                if result.text:
                    await self._send_text(conversation_id, result.text)
                await self._handle_conversation_agent_approval(conversation_id, result.approval)
            else:
                await self._send_text(conversation_id, (result.text.strip() or "已完成批准后的操作。"))
            self.approvals.finish_resume(approval_id, success=True)
        except AgentInvocationStopped as exc:
            self.approvals.finish_resume(approval_id, success=False)
            log.info("approval continuation stopped by operator approval=%s: %s", approval_id, exc)
        except Exception as exc:
            self.approvals.finish_resume(approval_id, success=False)
            log.exception("failed to resume Windows CodeAgent after approval %s", approval_row.get("approval_id"))
            await self._send_text(conversation_id, f"批准后的 Windows CodeAgent 继续执行失败：{exc}")

    @staticmethod
    def _format_runtime(seconds: float) -> str:
        total = max(0, int(seconds or 0))
        if total < 60:
            return f"{total}s"
        minutes, sec = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m{sec:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h{minutes:02d}m"

    async def _send_agent_sessions(self, conversation_id: str) -> None:
        stats = self.agents.scheduler_stats()
        rows = self.agents.active_agent_sessions()
        background_prefixes = ("maintenance", "workspace", "memory")
        fg = sum(1 for r in rows if not str(r.get("purpose") or "").startswith(background_prefixes))
        bg = len(rows) - fg
        lines = [f"CodeAgent 运行态：active={stats['active']}/{stats['max_concurrent']}，stopping={stats.get('stopping', 0)}，waiting={stats['waiting']}；foreground={fg}，background={bg}"]
        if not rows:
            lines.append("- 当前没有 active/stopping/waiting CodeAgent 调用。")
        for row in rows[:30]:
            inv = row.get("invocation_id") or "?"
            state = row.get("state") or "?"
            purpose = row.get("purpose") or "agent"
            lane = "BG" if str(purpose).startswith(background_prefixes) else "FG"
            elapsed = self._format_runtime(row.get("run_seconds") if state in {"active", "stopping"} else row.get("wait_seconds"))
            conv_id = str(row.get("conversation_id") or "")
            conv_name = ""
            if conv_id:
                conv = self.conversations.get(conv_id)
                if conv:
                    conv_name = str(conv["display_name"] or "")
            conv_part = f"；conv={conv_name or conv_id}" if conv_id else ""
            sess = str(row.get("session_id") or "")
            sess_part = f"；session={sess[:8]}…" if sess else ""
            detail = str(row.get("detail") or "").strip()
            detail_part = f"；{detail[:120]}" if detail else ""
            task_name = str(row.get("task_name") or "")
            task_part = f"；task={task_name[:100]}" if task_name else ""
            long_flag = " ⚠long" if state in {"active", "stopping"} and float(row.get("run_seconds") or 0) >= 900 else ""
            lines.append(f"- {inv} [{state}{long_flag}][{lane}] {purpose} {elapsed}{conv_part}{sess_part}{task_part}{detail_part}")
        if rows:
            lines.append("停止单个：/stop-session agent-...；全部停止：/stop-sessions all")
        await self._send_text(conversation_id, "\n".join(lines))

    async def _send_status(self, conversation_id: str) -> None:
        workflow = self.workflows.latest_for_conversation(conversation_id, active_only=True)
        task = self.tasks.latest_for_conversation(conversation_id, active_only=True)
        lines = []
        if workflow:
            steps = self.workflows.steps(workflow["workflow_id"])
            detail = "；".join(f"{s['step_id']}={s['state']}" for s in steps)
            lines.append(f"[{workflow['workflow_id']}] 工作流：{workflow['state']}。{detail}")
        if task and (not workflow or task["workflow_id"] != workflow["workflow_id"]):
            lines.append(f"[{task['task_id']}] {task['title'] or '远端任务'}：{task['state']}（节点 {task['node']}）")
        if not lines:
            latest_wf = self.workflows.latest_for_conversation(conversation_id)
            latest_task = self.tasks.latest_for_conversation(conversation_id)
            if latest_wf:
                lines.append(f"最近工作流 [{latest_wf['workflow_id']}]：{latest_wf['state']}")
            if latest_task:
                lines.append(f"最近任务 [{latest_task['task_id']}]：{latest_task['state']}（{latest_task['node']}）")
        sched = self.agents.scheduler_stats()
        if sched["active"] or sched.get("stopping") or sched["waiting"]:
            lines.append(f"CodeAgent：active={sched['active']}/{sched['max_concurrent']}，stopping={sched.get('stopping', 0)}，waiting={sched['waiting']}；用 /sessions 查看并可停止卡住的调用。")
        await self._send_text(conversation_id, "\n".join(lines) if lines else "当前会话没有任务、工作流或 CodeAgent 运行记录。")

    # ------------------------------------------------------------------
    # Task/workflow proposal + execution
    # ------------------------------------------------------------------
    def _find_quoted_execution_request(self, msg: IncomingMessage, text: str) -> dict | None:
        """Resolve an imperative like ``处理这个`` against WeLink quote metadata.

        Quote context is normally informational. It becomes a deterministic
        delegated execution request only when the CURRENT text is an execution
        imperative and the quoted text itself proves an execution scope.
        """
        quote = msg.quote if isinstance(msg.quote, dict) else None
        if not quote or not QUOTE_EXECUTION_RE.search(str(text or "").strip()):
            return None
        raw = str(quote.get("content") or "").strip()
        if not raw:
            return None
        matched_alias, stripped = self._strip_bot_alias(raw)
        instruction = stripped if matched_alias else raw
        if not instruction or instruction.startswith("/"):
            return None
        configured_nodes = tuple(self.cfg.get("nodes", {}).keys())
        scope = resolve_execution_scope(instruction, self.nodes.configs)
        if not (scope.requires_execution or is_explicit_remote_action(instruction, configured_nodes)
                or looks_like_mixed_workflow(instruction, configured_nodes)):
            return None
        return {
            "instruction": instruction,
            "scope": scope,
            "requested_by": str(quote.get("sender_id") or msg.sender_id),
            "origin_message_id": str(quote.get("message_id") or ""),
        }

    async def _handle_quoted_execution_request(self, msg: IncomingMessage, text: str, *, execution_allowed: bool, reply_allowed: bool) -> bool:
        ref = self._find_quoted_execution_request(msg, text)
        if not ref:
            return False
        if not execution_allowed:
            if reply_allowed and not self._silent_unfulfillable:
                await self._send_text(msg.conversation_id, "当前发送者没有执行权限，不能接管被引用的执行请求。")
            return True
        instruction = str(ref["instruction"]).strip()
        scope = ref["scope"]
        configured_nodes = tuple(self.cfg.get("nodes", {}).keys())
        provenance = {
            "requested_by": str(ref["requested_by"]),
            "authorized_by": msg.sender_id,
            "origin_message_id": str(ref.get("origin_message_id") or msg.external_message_id),
            "authorization_message_id": msg.external_message_id,
        }
        if scope.multi_target and scope.requires_execution:
            await self._propose_workflow(msg, instruction, scope=scope, **provenance)
            return True
        if looks_like_mixed_workflow(instruction, configured_nodes):
            await self._propose_workflow(msg, instruction, scope=scope, **provenance)
            return True
        if is_explicit_remote_action(instruction, configured_nodes) or scope.remote_targets:
            node = configured_node_in_text(instruction, configured_nodes) or (scope.remote_targets[0] if scope.remote_targets else None)
            await self._propose_remote_task(msg, instruction, node=node, **provenance)
            return True
        return False

    def _find_delegated_execution_request(self, msg: IncomingMessage, text: str) -> dict | None:
        """Resolve ``处理上面 <account> 的任务`` to a durable earlier message.

        The referenced message may have been stored while its author had no
        execution permission.  A later operator can authorize it without making
        the model pretend that execution already happened.
        """
        match = DELEGATED_TASK_RE.search(str(text or ""))
        if not match:
            return None
        sender = str(match.group("sender") or "").strip()
        if not sender:
            return {"matched": True, "error": "未识别到被引用的发送者账号。"}
        candidates = self.conversations.recent_messages_from_sender(
            msg.conversation_id, sender,
            before_external_message_id=msg.external_message_id,
            limit=20,
        )
        configured_nodes = tuple(self.cfg.get("nodes", {}).keys())
        for row in candidates:
            raw = str(row.get("content") or "").strip()
            if not raw:
                continue
            matched_alias, stripped = self._strip_bot_alias(raw)
            instruction = stripped if matched_alias else raw
            if not instruction or instruction.startswith("/"):
                continue
            scope = resolve_execution_scope(instruction, self.nodes.configs)
            if (scope.requires_execution or is_explicit_remote_action(instruction, configured_nodes)
                    or looks_like_mixed_workflow(instruction, configured_nodes)):
                return {
                    "matched": True,
                    "requested_by": str(row.get("sender_id") or sender),
                    "origin_message_id": str(row.get("external_message_id") or ""),
                    "instruction": instruction,
                    "scope": scope,
                }
        return {
            "matched": True,
            "error": f"没有在当前会话中找到 {sender} 最近可确定为执行任务的消息。",
        }

    async def _handle_delegated_execution_request(self, msg: IncomingMessage, text: str, *, execution_allowed: bool, reply_allowed: bool) -> bool:
        ref = self._find_delegated_execution_request(msg, text)
        if not ref:
            return False
        if not execution_allowed:
            if reply_allowed and not self._silent_unfulfillable:
                await self._send_text(msg.conversation_id, "当前发送者没有执行权限，不能接管他人的任务。")
            return True
        if ref.get("error"):
            await self._send_text(msg.conversation_id, str(ref["error"]))
            return True

        instruction = str(ref["instruction"]).strip()
        requested_by = str(ref["requested_by"])
        scope = ref["scope"]
        configured_nodes = tuple(self.cfg.get("nodes", {}).keys())
        provenance = {
            "requested_by": requested_by,
            "authorized_by": msg.sender_id,
            "origin_message_id": str(ref.get("origin_message_id") or ""),
            "authorization_message_id": msg.external_message_id,
        }
        if scope.multi_target and scope.requires_execution:
            await self._propose_workflow(msg, instruction, scope=scope, **provenance)
            return True
        if looks_like_mixed_workflow(instruction, configured_nodes):
            await self._propose_workflow(msg, instruction, scope=scope, **provenance)
            return True
        if is_explicit_remote_action(instruction, configured_nodes) or scope.remote_targets:
            node = configured_node_in_text(instruction, configured_nodes)
            if node is None and len(scope.remote_targets) == 1:
                node = scope.remote_targets[0]
            await self._propose_remote_task(msg, instruction, node=node, **provenance)
            return True

        # Do not silently reinterpret an ambiguous delegated request as a local
        # side effect.  Ask the operator to restate it so deterministic routing
        # and policy can evaluate the actual execution scope.
        await self._send_text(
            msg.conversation_id,
            f"已找到 {requested_by} 的原始请求：{preview(instruction)}\n但当前无法确定它的远端/多节点执行范围，请直接复述要执行的任务。",
        )
        return True

    async def _propose_remote_task(self, msg: IncomingMessage, instruction: str, *,
                                   node: str | None = None, recommendation: bool = False,
                                   requested_by: str | None = None, authorized_by: str | None = None,
                                   origin_message_id: str | None = None,
                                   authorization_message_id: str | None = None):
        node = node if node in self.cfg.get("nodes", {}) else self.nodes.choose(instruction, preferred=self.cfg.get("default_node"))
        if not node or node not in self.cfg.get("nodes", {}):
            await self._send_text(msg.conversation_id, "当前没有配置可用开发服务器。")
            return
        lead = "建议" if recommendation else "将"
        requested_by = str(requested_by or msg.sender_id)
        authorized_by = str(authorized_by or msg.sender_id)
        origin_message_id = str(origin_message_id or msg.external_message_id)
        authorization_message_id = str(authorization_message_id or msg.external_message_id)
        provenance = ""
        if requested_by != authorized_by or origin_message_id != authorization_message_id:
            provenance = f"\n原始请求者：{requested_by}；执行授权者：{authorized_by}"
        prompt = f"{lead}在 {node} 创建远端 CodeAgent 任务。\n任务内容：{preview(instruction)}{provenance}\n是否创建？"
        action = PendingAction(
            action_type="task.create",
            payload={"node": node, "title": "远端任务", "instruction": instruction.strip(),
                     "origin_message_id": origin_message_id,
                     "requested_by": requested_by, "authorized_by": authorized_by,
                     "authorization_message_id": authorization_message_id},
            prompt=prompt,
        )
        self.conversations.set_pending_action(msg.conversation_id, action)
        await self._send_text(msg.conversation_id, action.prompt)

    async def _propose_workflow(self, msg: IncomingMessage, instruction: str, recommendation: bool = False,
                                scope: ExecutionScope | None = None, *,
                                requested_by: str | None = None, authorized_by: str | None = None,
                                origin_message_id: str | None = None,
                                authorization_message_id: str | None = None):
        scope = scope or resolve_execution_scope(instruction, self.nodes.configs)
        requested_by = str(requested_by or msg.sender_id)
        authorized_by = str(authorized_by or msg.sender_id)
        origin_message_id = str(origin_message_id or msg.external_message_id)
        authorization_message_id = str(authorization_message_id or msg.external_message_id)
        try:
            plan = await self.agents.plan_workflow(
                msg.conversation_id, instruction, self.nodes.planner_configs(),
                required_targets=scope.targets if scope.multi_target else (),
                fresh_execution=scope.fresh_execution,
            )
        except Exception as exc:
            log.exception("workflow planning failed")
            await self._send_text(msg.conversation_id, f"工作流规划失败：{exc}")
            return
        lines = []
        for step in plan.steps:
            where = "Windows" if step.executor == "windows" else step.node
            deps = f"，依赖 {', '.join(step.depends_on)}" if step.depends_on else ""
            lines.append(f"- {step.step_id} @ {where}{deps}：{preview(step.instruction, 150)}")
        lead = "建议创建" if recommendation else "将创建"
        label = "多节点工作流" if scope.multi_target else "工作流"
        # Node names are rendered from framework-authoritative structured scope,
        # never inferred from the model's prose summary (which can typo a node).
        target_line = ""
        if scope.targets:
            target_line = "\n目标节点：" + ", ".join(scope.targets)
        offline_required = [
            target for target in scope.remote_targets
            if target in self.nodes.configs and not self.nodes.is_connected(target)
        ]
        warning = (f"\n注意：当前离线目标：{', '.join(offline_required)}；WorkBot 不会静默改派到其它节点。"
                   if offline_required else "")
        # For multi-target plans, render the user request itself as the headline
        # instead of model-authored prose that may typo a node name (e.g. dev93).
        headline = preview(instruction, 180) if scope.multi_target else plan.summary
        provenance = ""
        if requested_by != authorized_by or origin_message_id != authorization_message_id:
            provenance = f"\n原始请求者：{requested_by}；执行授权者：{authorized_by}"
        prompt = f"{lead}{label}：{headline}{target_line}{provenance}\n" + "\n".join(lines) + warning + "\n是否执行？"
        action = PendingAction(
            action_type="workflow.create",
            payload={
                "instruction": instruction,
                "origin_message_id": origin_message_id,
                "requested_by": requested_by,
                "authorized_by": authorized_by,
                "authorization_message_id": authorization_message_id,
                "plan": {"summary": plan.summary, "steps": [
                    {"step_id": s.step_id, "executor": s.executor, "node": s.node,
                     "instruction": s.instruction, "depends_on": s.depends_on,
                     "notify_on_complete": s.notify_on_complete, "milestone": s.milestone}
                    for s in plan.steps
                ]},
            },
            prompt=prompt,
        )
        self.conversations.set_pending_action(msg.conversation_id, action)
        await self._send_text(msg.conversation_id, action.prompt)

    async def _execute_pending(self, msg: IncomingMessage, pending: PendingAction, *, already_consumed: bool = False):
        if pending.action_type == "task.create":
            p = pending.payload
            try:
                task_id = await self.tasks.create_remote_task(
                    node=p["node"], conversation_id=msg.conversation_id,
                    origin_message_id=p.get("origin_message_id"), title=p.get("title", "远端任务"),
                    instruction=p["instruction"],
                    requested_by=p.get("requested_by") or msg.sender_id,
                    authorized_by=p.get("authorized_by") or msg.sender_id,
                    authorization_message_id=p.get("authorization_message_id") or msg.external_message_id,
                )
                if not already_consumed:
                    self.conversations.set_pending_action(msg.conversation_id, None)
                await self._send_text(msg.conversation_id, f"已创建远端任务 {task_id}，正在 {p['node']} 执行。")
            except Exception as exc:
                log.exception("failed to create remote task")
                if already_consumed:
                    self.conversations.set_pending_action(msg.conversation_id, pending)
                await self._send_text(msg.conversation_id, f"任务创建失败：{exc}")
            return

        if pending.action_type == "agent.peer_request":
            p = pending.payload
            try:
                message_id = await self._send_collaboration_message(
                    msg.conversation_id, str(p["peer_id"]), str(p["instruction"]), message_type="request", hop=0
                )
                if not already_consumed:
                    self.conversations.set_pending_action(msg.conversation_id, None)
                await self._send_text(msg.conversation_id, f"已向 peer Agent {p['peer_id']} 发送协作请求 {message_id}。")
            except Exception as exc:
                log.exception("failed to send peer Agent collaboration request")
                if already_consumed:
                    self.conversations.set_pending_action(msg.conversation_id, pending)
                await self._send_text(msg.conversation_id, f"peer Agent 协作请求发送失败：{exc}")
            return

        if pending.action_type in {"task.steer", "task.append"}:
            p = pending.payload
            mode = "steer" if pending.action_type == "task.steer" else "append"
            try:
                result = await self.tasks.add_instruction(p["task_id"], p["instruction"], mode=mode)
                if not already_consumed:
                    self.conversations.set_pending_action(msg.conversation_id, None)
                if mode == "steer":
                    await self._send_text(msg.conversation_id, f"已调整 {p['task_id']}，正在同一 session 的 turn {result.get('turn')} 执行新指令。")
                else:
                    await self._send_text(msg.conversation_id, f"已向 {p['task_id']} 追加 instruction #{result.get('sequence')}。")
            except Exception as exc:
                if already_consumed:
                    self.conversations.set_pending_action(msg.conversation_id, pending)
                await self._send_text(msg.conversation_id, f"任务指令更新失败：{exc}")
            return

        if pending.action_type == "workflow.create":
            p = pending.payload
            plan = WorkflowPlan(summary=p["plan"]["summary"], steps=[WorkflowStep(**x) for x in p["plan"]["steps"]])
            try:
                workflow_id = self.workflows.create(
                    conversation_id=msg.conversation_id, origin_message_id=p.get("origin_message_id"),
                    instruction=p["instruction"], plan=plan,
                    requested_by=p.get("requested_by") or msg.sender_id,
                    authorized_by=p.get("authorized_by") or msg.sender_id,
                    authorization_message_id=p.get("authorization_message_id") or msg.external_message_id,
                )
            except Exception as exc:
                log.exception("failed to create workflow")
                if already_consumed:
                    self.conversations.set_pending_action(msg.conversation_id, pending)
                await self._send_text(msg.conversation_id, f"工作流创建失败：{exc}")
                return
            if not already_consumed:
                self.conversations.set_pending_action(msg.conversation_id, None)
            await self._send_text(msg.conversation_id,
                f"已创建工作流 {workflow_id}，共 {len(plan.steps)} 个步骤，开始执行。")
            self._spawn_workflow(workflow_id,
                self._run_workflow(workflow_id, msg.conversation_id, p["instruction"], plan))
            return

        if not already_consumed:
            self.conversations.set_pending_action(msg.conversation_id, None)
        await self._send_text(msg.conversation_id, f"暂不支持待执行操作：{pending.action_type}")

    async def _run_workflow(self, workflow_id: str, conversation_id: str,
                            original_instruction: str, plan: WorkflowPlan, *, recovering: bool = False):
        self.workflows.set_workflow_state(workflow_id, "recovering" if recovering else "running")
        step_by_id = {s.step_id: s for s in plan.steps}
        stored = {s["step_id"]: s for s in self.workflows.steps(workflow_id)}
        results: dict[str, str] = {
            sid: str(row["result_text"] or "") for sid, row in stored.items() if row["state"] == "completed"
        }
        pending = {sid for sid in step_by_id if sid not in results}
        try:
            while pending:
                stored = {s["step_id"]: s for s in self.workflows.steps(workflow_id)}
                bad = [sid for sid in pending if stored[sid]["state"] in {"failed", "interrupted"}]
                if bad:
                    raise RuntimeError(f"工作流包含需要人工处理的步骤：{', '.join(bad)}")
                if any(stored[sid]["state"] == "cancelled" for sid in pending):
                    raise asyncio.CancelledError()
                ready = [step_by_id[sid] for sid in pending if set(step_by_id[sid].depends_on).issubset(results)]
                if not ready:
                    raise RuntimeError("工作流依赖存在环或前置步骤未完成")
                outcomes = await asyncio.gather(
                    *(self._run_workflow_step(workflow_id, conversation_id, step, results) for step in ready),
                    return_exceptions=True,
                )
                for step, outcome in zip(ready, outcomes):
                    if isinstance(outcome, asyncio.CancelledError):
                        raise outcome
                    if isinstance(outcome, Exception):
                        self.workflows.set_step_state(workflow_id, step.step_id, "failed", result_text=str(outcome))
                        raise RuntimeError(f"{step.step_id} 执行失败：{outcome}") from outcome
                    results[step.step_id] = outcome
                    pending.remove(step.step_id)
                    if step.notify_on_complete:
                        milestone = step.milestone or f"{step.step_id} 已完成"
                        await self._send_text(conversation_id, f"[{workflow_id}] 阶段进展：{milestone}")

            ordered_results = [(s.step_id, results[s.step_id]) for s in plan.steps]
            final = await self.agents.synthesize_workflow(conversation_id, original_instruction, plan.summary, ordered_results)
            self.workflows.set_workflow_state(workflow_id, "completed", {"results": results, "summary": final})
            await self._send_text(conversation_id, f"[{workflow_id}] 工作流完成。\n{final}")
            if self._auto_memory_source("workflow"):
                raw_results = "\n\n".join(f"[{sid}]\n{text}" for sid, text in ordered_results)
                material = (
                    f"Original request:\n{original_instruction}\n\n"
                    f"Raw workflow step results (authoritative task outputs):\n{raw_results}\n\n"
                    f"Final synthesized answer (secondary):\n{final}"
                )
                self._schedule_memory_capture(
                    conversation_id, material, source=f"workflow:{workflow_id}", source_kind="workflow"
                )
        except asyncio.CancelledError:
            log.info("workflow %s runner cancelled", workflow_id)
            raise
        except Exception as exc:
            log.exception("workflow %s failed", workflow_id)
            self.workflows.set_workflow_state(workflow_id, "failed", {"error": str(exc), "results": results})
            await self._send_text(conversation_id, f"[{workflow_id}] 工作流失败：{exc}")

    async def _steer_workflow_step(self, workflow_id: str, step_id: str, instruction: str, *, mode: str) -> str:
        row = self.workflows.step(workflow_id, step_id)
        if not row:
            raise KeyError(f"unknown workflow step {workflow_id}/{step_id}")
        if row["state"] != "running":
            raise RuntimeError(f"{workflow_id}/{step_id} 当前状态为 {row['state']}，只能调整正在运行的步骤")
        if row["executor"] == "remote":
            task_id = row["task_id"]
            if not task_id:
                raise RuntimeError("远端步骤尚未创建 child task")
            result = await self.tasks.add_instruction(task_id, instruction, mode=mode)
            return (
                f"远端 child task {task_id} 已在同一 CodeAgent session 中"
                + (f"切换到 turn {result.get('turn')}" if mode == "steer" else f"排队 instruction #{result.get('sequence')}")
            )
        key = (workflow_id, step_id)
        queue = self._windows_step_instruction_queues.setdefault(key, [])
        queue.append((mode, instruction))
        if mode == "steer":
            current = self._windows_step_agent_tasks.get(key)
            if current and not current.done():
                current.cancel()
                return "Windows 当前 CodeAgent turn 已请求中断；将用同一 Conversation session 立即执行纠偏指令"
            return "Windows 步骤当前不在 Agent 调用中；纠偏指令已排到下一 turn 前"
        return "已追加到 Windows 步骤；当前 turn 完成后在同一 Conversation session 中继续"

    async def _await_windows_step_agent(self, key: tuple[str, str], conversation_id: str, first_coro, *, context: str):
        """Run one Windows step invocation while allowing /steer and /add.

        Only the *inner* CodeAgent task is cancelled for steering. The outer
        workflow-step coroutine survives, so the workflow itself does not get
        cancelled when the user redirects a long-running Windows turn.
        """
        next_coro = first_coro
        while True:
            # A steering instruction queued while the step was waiting for an
            # approval should supersede the pending continuation before we start
            # another Agent invocation. Appends do not preempt an approved action.
            queue = self._windows_step_instruction_queues.get(key, [])
            steer_index = next((i for i, item in enumerate(queue) if item[0] == "steer"), None)
            if steer_index is not None:
                _mode, steer_instruction = queue.pop(steer_index)
                next_coro = lambda inst=steer_instruction: self.agents.continue_task_instruction(
                    conversation_id, instruction=inst, mode="steer", context=context
                )
            agent_task = asyncio.create_task(next_coro(), name=f"windows-step-agent:{key[0]}:{key[1]}")
            self._windows_step_agent_tasks[key] = agent_task
            try:
                result = await agent_task
            except asyncio.CancelledError:
                queue = self._windows_step_instruction_queues.get(key, [])
                steer_index = next((i for i, item in enumerate(queue) if item[0] == "steer"), None)
                if steer_index is None:
                    raise
                _mode, steer_instruction = queue.pop(steer_index)
                next_coro = lambda inst=steer_instruction: self.agents.continue_task_instruction(
                    conversation_id, instruction=inst, mode="steer", context=context
                )
                continue
            finally:
                if self._windows_step_agent_tasks.get(key) is agent_task:
                    self._windows_step_agent_tasks.pop(key, None)
            # Approval must be handled by the outer workflow logic before an
            # appended follow-up can execute. A newer steer still supersedes it.
            queue = self._windows_step_instruction_queues.get(key, [])
            steer_index = next((i for i, item in enumerate(queue) if item[0] == "steer"), None)
            if steer_index is not None:
                _mode, steer_instruction = queue.pop(steer_index)
                next_coro = lambda inst=steer_instruction: self.agents.continue_task_instruction(
                    conversation_id, instruction=inst, mode="steer", context=context
                )
                continue
            if result.approval:
                return result
            append_index = next((i for i, item in enumerate(queue) if item[0] == "append"), None)
            if append_index is not None:
                _mode, append_instruction = queue.pop(append_index)
                next_coro = lambda inst=append_instruction: self.agents.continue_task_instruction(
                    conversation_id, instruction=inst, mode="append", context=context
                )
                continue
            if not queue:
                self._windows_step_instruction_queues.pop(key, None)
            return result

    async def _run_workflow_step(self, workflow_id: str, conversation_id: str, step, results: dict[str, str]) -> str:
        current = self.workflows.step(workflow_id, step.step_id)
        if current and current["state"] == "completed":
            return str(current["result_text"] or "")
        dependency_context = "\n\n".join(
            f"[{dep}]\n{results[dep][-5000:]}" for dep in step.depends_on if dep in results
        )
        try:
            if step.executor == "windows":
                if current and current["state"] == "interrupted":
                    raise RuntimeError("Windows 步骤在 WorkBot 重启时被中断，需要用户明确恢复后才可重跑")
                self.workflows.set_step_state(workflow_id, step.step_id, "running", increment_attempt=True)
                key = (workflow_id, step.step_id)
                agent_result = await self._await_windows_step_agent(
                    key, conversation_id,
                    lambda: self.agents.execute_local_step(conversation_id, step.instruction, dependency_context),
                    context=f"workflow={workflow_id}, step={step.step_id}",
                )
                while agent_result.approval:
                    approval = agent_result.approval
                    action = str(approval.get("action") or "").strip().lower()
                    summary = str(approval.get("summary") or action or "Windows workflow action").strip()
                    policy_decision = self.policy.evaluate_action(action)
                    if policy_decision.decision == "deny":
                        raise RuntimeError(f"Windows 操作被策略拒绝：{action}（{policy_decision.reason}）")
                    if policy_decision.decision == "approve":
                        aid = self.approvals.create(
                            conversation_id=conversation_id, node="office-pc", task_id=None, action=action, summary=summary,
                            details={"agent_details": approval.get("details") or {}, "_resume_kind": "workflow",
                                     "workflow_id": workflow_id, "step_id": step.step_id},
                            requested_by="windows-codeagent",
                        )
                        await self._send_text(
                            conversation_id,
                            f"[{aid}] Windows 工作流步骤请求批准操作\n动作：{action}\n工作流：{workflow_id}/{step.step_id}\n说明：{summary}\n"
                            f"批准：/approve {aid}\n拒绝：/deny {aid} [原因]",
                        )
                        approval_result = await self.approvals.wait(aid)
                        if not approval_result.approved:
                            raise RuntimeError(f"Windows 操作未获批准：{action}（{approval_result.decision}）")
                    agent_result = await self._await_windows_step_agent(
                        key, conversation_id,
                        lambda a=action, sm=summary: self.agents.continue_after_approval(
                            conversation_id, action=a, summary=sm,
                            context=f"workflow={workflow_id}, step={step.step_id}",
                            approval_token=self._create_welink_approval_token(a),
                        ),
                        context=f"workflow={workflow_id}, step={step.step_id}",
                    )
                output = agent_result.text.strip()
                self.workflows.set_step_state(workflow_id, step.step_id, "completed", result_text=output)
                return output

            # Recovery: if a remote child task already exists, continue tracking
            # it instead of creating a duplicate task.
            task_id = current["task_id"] if current else None
            if task_id:
                task_row = self.tasks.get(task_id)
            else:
                task_row = None
            if not task_row or task_row["state"] not in {"running", "created", "cancelling", "completed", "failed", "cancelled"}:
                task_id = None
            if not task_id:
                remote_instruction = step.instruction
                if dependency_context:
                    remote_instruction += "\n\n--- Results from prerequisite workflow steps ---\n" + dependency_context + "\n--- End prerequisite results ---"
                self.workflows.set_step_state(workflow_id, step.step_id, "running", increment_attempt=True)
                task_id = await self.tasks.create_remote_task(
                    node=step.node, conversation_id=conversation_id, origin_message_id=None,
                    title=f"工作流步骤 {step.step_id}", instruction=remote_instruction,
                    workflow_id=workflow_id, workflow_step_id=step.step_id, notify_mode="workflow",
                )
                self.workflows.set_step_state(workflow_id, step.step_id, "running", task_id=task_id)
            row = await self.tasks.wait_for_terminal(task_id, timeout=self._workflow_step_timeout)
            payload = self.store.loads(row["result_json"], {})
            summary = str(payload.get("summary") or payload.get("error") or "")
            if row["state"] != "completed":
                raise RuntimeError(summary or f"remote task {task_id} state={row['state']}")
            self.workflows.set_step_state(workflow_id, step.step_id, "completed", result_text=summary, task_id=task_id)
            return summary
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.workflows.set_step_state(workflow_id, step.step_id, "failed", result_text=str(exc))
            raise

    # ------------------------------------------------------------------
    # V0.4 workflow recovery/cancel/retry
    # ------------------------------------------------------------------
    async def _recover_workflows(self) -> None:
        for wf in self.workflows.active():
            wf_id = wf["workflow_id"]
            if wf["state"] == "blocked":
                continue
            if wf["state"] == "cancelling":
                await self._cancel_workflow(wf_id, notify=False)
                continue
            interrupted = self.workflows.mark_interrupted_windows_steps(wf_id)
            if interrupted:
                reason = "WorkBot 重启时 Windows 步骤仍在运行：" + ", ".join(interrupted)
                self.workflows.set_workflow_state(wf_id, "blocked", recovery_reason=reason)
                if wf["origin_conversation_id"]:
                    await self._send_text(wf["origin_conversation_id"],
                        f"[{wf_id}] WorkBot 已恢复，但 {', '.join(interrupted)} 是重启时中断的 Windows 步骤。"
                        "为避免重复副作用，未自动重跑。发送 `/resume " + wf_id + "` 可明确继续。")
                continue
            try:
                plan = self.workflows.plan_from_store(wf_id)
                self._spawn_workflow(wf_id, self._run_workflow(
                    wf_id, wf["origin_conversation_id"], wf["original_instruction"], plan, recovering=True))
                log.info("recovering workflow %s", wf_id)
            except Exception:
                log.exception("failed to recover workflow %s", wf_id)

    async def _cancel_workflow(self, workflow_id: str, *, notify: bool) -> None:
        wf = self.workflows.get(workflow_id)
        if not wf:
            raise KeyError(f"unknown workflow {workflow_id}")
        if wf["state"] in {"completed", "failed", "cancelled"}:
            if notify and wf["origin_conversation_id"]:
                await self._send_text(wf["origin_conversation_id"], f"{workflow_id} 已是终态：{wf['state']}。")
            return
        self.workflows.set_workflow_state(workflow_id, "cancelling")
        runner = self._workflow_runners.get(workflow_id)
        if runner and not runner.done() and runner is not asyncio.current_task():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        for step in self.workflows.steps(workflow_id):
            if step["task_id"]:
                task = self.tasks.get(step["task_id"])
                if task and task["state"] in {"created", "running", "cancelling"}:
                    try:
                        await self.tasks.cancel(task["task_id"])
                    except Exception:
                        log.exception("failed to cancel child task %s", task["task_id"])
            if step["state"] not in {"completed", "failed"}:
                self.workflows.set_step_state(workflow_id, step["step_id"], "cancelled")
        self.workflows.set_workflow_state(workflow_id, "cancelled", {"cancelled": True})
        if notify and wf["origin_conversation_id"]:
            await self._send_text(wf["origin_conversation_id"], f"已取消工作流 {workflow_id}。")

    async def _resume_blocked_workflow(self, workflow_id: str) -> None:
        wf = self.workflows.get(workflow_id)
        if not wf:
            raise KeyError(workflow_id)
        if wf["state"] != "blocked":
            raise RuntimeError(f"workflow state={wf['state']}，无需 resume")
        for step in self.workflows.steps(workflow_id):
            if step["state"] == "interrupted":
                self.workflows.reset_step(workflow_id, step["step_id"])
        self.workflows.set_workflow_state(workflow_id, "recovering")
        plan = self.workflows.plan_from_store(workflow_id)
        self._spawn_workflow(workflow_id,
            self._run_workflow(workflow_id, wf["origin_conversation_id"], wf["original_instruction"], plan, recovering=True))

    async def _retry_workflow_step(self, workflow_id: str, step_id: str) -> None:
        wf = self.workflows.get(workflow_id)
        if not wf:
            raise KeyError(workflow_id)
        steps = self.workflows.steps(workflow_id)
        if step_id not in {s["step_id"] for s in steps}:
            raise KeyError(f"unknown step {step_id}")
        # Reset the selected step and every downstream step because their old
        # outputs may depend on the result being recomputed.
        reset = {step_id}
        changed = True
        while changed:
            changed = False
            for s in steps:
                if s["step_id"] not in reset and any(dep in reset for dep in s["depends_on"]):
                    reset.add(s["step_id"]); changed = True
        for sid in reset:
            row = next(s for s in steps if s["step_id"] == sid)
            if row["task_id"]:
                task = self.tasks.get(row["task_id"])
                if task and task["state"] in {"created", "running", "cancelling"}:
                    await self.tasks.cancel(task["task_id"])
            self.workflows.reset_step(workflow_id, sid)
        self.workflows.set_workflow_state(workflow_id, "recovering")
        plan = self.workflows.plan_from_store(workflow_id)
        self._spawn_workflow(workflow_id,
            self._run_workflow(workflow_id, wf["origin_conversation_id"], wf["original_instruction"], plan, recovering=True))

    # ------------------------------------------------------------------
    # Remote protocol/event routing
    # ------------------------------------------------------------------
    async def on_remote_message(self, node: str, msg: Message):
        if msg.type == "response":
            self.tasks.handle_response(msg)
            return
        if msg.type == "request":
            # Never block the SSH stdout reader on a Windows CodeAgent/office
            # lookup. Responses/events for normal remote tasks must continue to
            # flow while an RPC is in progress.
            self._spawn(self._handle_node_request(node, msg), name=f"rpc:{node}:{msg.id}")
            return
        if msg.type == "event":
            is_new = self.tasks.handle_event(node, msg)
            ack = Message(type="ack", correlation_id=msg.id, source="workbot", target=node,
                          data={"event_id": msg.id})
            try:
                await self.ssh.send(node, ack)
            except Exception:
                log.exception("failed to ACK %s", msg.id)
            if is_new:
                await self._route_event(node, msg)

    async def _handle_node_request(self, node: str, msg: Message) -> None:
        method = str(msg.method or "")
        try:
            if method == "policy.request_approval":
                result = await self._handle_approval_request(node, msg)
            else:
                result = await self.rpc.execute(node, method, msg.data, task_id=msg.task_id)
            data = {"ok": True, "result": result}
        except Exception as exc:
            log.warning("RPC %s from %s failed: %s", method, node, exc)
            data = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        reply = Message(
            type="response", correlation_id=msg.id, task_id=msg.task_id,
            source="workbot", target=node, data=data,
        )
        try:
            await self.ssh.send(node, reply)
        except Exception:
            log.exception("failed to send RPC response %s to %s", msg.id, node)

    async def _handle_approval_request(self, node: str, msg: Message) -> dict:
        action = str(msg.data.get("action") or "").strip().lower()
        summary = str(msg.data.get("summary") or action or "未说明操作").strip()
        details = msg.data.get("details") if isinstance(msg.data.get("details"), dict) else {}
        task = self.tasks.get(msg.task_id) if msg.task_id else None
        conversation_id = str(msg.data.get("conversation_id") or "").strip()
        if not conversation_id and task:
            conversation_id = str(task["origin_conversation_id"] or "")
        if not conversation_id:
            conversation_id = str((self.cfg.get("nodes", {}).get(node, {}) or {}).get("default_conversation_id") or "")
        if not conversation_id:
            raise ValueError("approval request has no target conversation")

        decision = self.policy.evaluate_action(action)
        if decision.decision == "allow":
            return {
                "approval_id": None, "decision": "approved", "approved": True, "auto": True,
                "reason": decision.reason, "action": action,
            }
        if decision.decision == "deny":
            return {
                "approval_id": None, "decision": "denied", "approved": False, "auto": True,
                "reason": decision.reason, "action": action,
            }

        requested_by = str(msg.data.get("requested_by") or node)
        timeout = float(msg.data.get("timeout_seconds") or self.approvals.timeout_seconds)
        approval_id = self.approvals.create(
            conversation_id=conversation_id, node=node, task_id=msg.task_id, action=action,
            summary=summary, details=details, requested_by=requested_by, timeout_seconds=timeout,
        )
        detail_text = ""
        if details:
            raw = json.dumps(details, ensure_ascii=False)
            if len(raw) > 1200:
                raw = raw[:1200] + "…"
            detail_text = f"\n详情：{raw}"
        await self._send_text(
            conversation_id,
            f"[{approval_id}] {node} 请求批准操作\n"
            f"动作：{action}\n"
            + (f"任务：{msg.task_id}\n" if msg.task_id else "")
            + f"说明：{summary}{detail_text}\n"
            f"批准：/approve {approval_id}\n拒绝：/deny {approval_id} [原因]",
        )
        result = await self.approvals.wait(approval_id, timeout=timeout)
        return {
            "approval_id": approval_id, "decision": result.decision, "approved": result.approved,
            "reason": result.reason, "decided_by": result.decided_by, "action": action, "auto": False,
        }

    async def _route_event(self, node: str, msg: Message):
        conversation_id = None
        task = self.tasks.get(msg.task_id) if msg.task_id else None
        if task:
            conversation_id = task["origin_conversation_id"]
        if not conversation_id:
            conversation_id = msg.data.get("conversation_id")
        if not conversation_id:
            conversation_id = (self.cfg.get("nodes", {}).get(node, {}) or {}).get("default_conversation_id")
        if not conversation_id:
            log.warning("No notification target for event %s from %s", msg.id, node)
            return

        if msg.event == "policy.action.executed":
            log.info("Recorded policy execution audit event for %s", msg.task_id or node)
            return
        if msg.event and msg.event.startswith("task.turn."):
            log.info("Recorded internal task-turn event %s for %s", msg.event, msg.task_id)
            return

        if msg.task_id and msg.event in {"agent.notification", "test.completed", "test.failed"}:
            log.info("Suppressing task-scoped auxiliary event %s for %s", msg.event, msg.task_id)
            return

        summary = str(msg.data.get("summary") or msg.data.get("error") or msg.event or "远端事件")
        if len(summary) > 2500:
            summary = summary[-2500:]

        if task and task["notify_mode"] == "workflow":
            if msg.event in {"task.completed", "task.failed", "task.cancelled", "task.started"}:
                return
            wf = task["workflow_id"] or "workflow"
            step = task["workflow_step_id"] or "step"
            if msg.event == "task.progress":
                head = f"[{wf}/{step}] {node} 阶段进展。"
            elif msg.event == "task.blocked":
                head = f"[{wf}/{step}] {node} 任务需要处理。"
            else:
                head = f"[{wf}/{step}] {node}: {msg.event}"
            await self._send_text(conversation_id, f"{head}\n{summary}")
            return

        if msg.task_id:
            if msg.event == "task.completed":
                head = f"[{msg.task_id}] {node} 任务完成。"
            elif msg.event == "task.failed":
                head = f"[{msg.task_id}] {node} 任务失败。"
            elif msg.event == "task.cancelled":
                head = f"[{msg.task_id}] {node} 任务已取消。"
            elif msg.event == "task.started":
                return
            elif msg.event == "task.progress":
                head = f"[{msg.task_id}] {node} 任务进度。"
            elif msg.event == "task.blocked":
                head = f"[{msg.task_id}] {node} 任务需要处理。"
            else:
                head = f"[{msg.task_id}] {node}: {msg.event}"
        else:
            head = f"[{node}] {msg.event or '通知'}"
        await self._send_text(conversation_id, f"{head}\n{summary}")


def load_args():
    ap = argparse.ArgumentParser(prog="workbot")
    ap.add_argument("--config", default="config/local.json")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args()


async def amain(args):
    cfg = Path(args.config)
    bot = WorkBot(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:
            pass
    await bot.run()


def cli():
    args = load_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
