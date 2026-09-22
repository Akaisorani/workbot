from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from workbot.control.runtime import RuntimeController
from workbot.setup.agents_generator import generate_agents_file
from workbot.setup.configurator import backup_config, load_config, save_config_atomic, validate_config
from workbot.setup.doctor import run_doctor


HOT_RELOAD_SECTIONS = {"ingestion_policy", "reply_policy", "execution_policy"}


def _csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").replace("\n", ",").split(",") if x.strip()]


def _nested(cfg: dict, *keys, default=None):
    cur = cfg
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


class WorkBotGUI:
    def __init__(self, root: tk.Tk, config_path: str | Path):
        self.root = root
        self.config_path = Path(config_path).resolve()
        self.workspace = self.config_path.parent.parent
        self.controller = RuntimeController(self.config_path)
        self.snapshot: dict = {"controller": {"state": "stopped"}}
        self._snapshot_busy = False
        self._last_log_seq = 0
        self._closing = False

        self.root.title("WorkBot Control Center")
        self.root.geometry("1280x820")
        self.root.minsize(1050, 680)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self._load_config_into_editor()
        self._schedule_refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="WorkBot Control Center", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="STOPPED")
        ttk.Label(toolbar, textvariable=self.status_var).pack(side="left", padx=(18, 10))
        self.uptime_var = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.uptime_var).pack(side="left", padx=(0, 14))
        ttk.Button(toolbar, text="Start", command=self._start).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Stop", command=self._stop).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Restart", command=self._restart).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Doctor", command=self._run_doctor).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Refresh", command=self._refresh_now).pack(side="left", padx=3)
        ttk.Label(toolbar, text=str(self.config_path)).pack(side="right")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._build_dashboard_tab()
        self._build_agent_tab()
        self._build_tasks_tab()
        self._build_workflows_tab()
        self._build_nodes_peers_tab()
        self._build_rag_tab()
        self._build_config_tab()
        self._build_logs_tab()
        self._build_doctor_tab()

    def _add_tab(self, title: str) -> ttk.Frame:
        frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(frame, text=title)
        return frame

    @staticmethod
    def _tree(parent, columns, widths=None, height=12):
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=height)
        widths = widths or {}
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=widths.get(col, 120), anchor="w", stretch=True)
        sy = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sy.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        return tree

    def _build_dashboard_tab(self):
        tab = self._add_tab("Dashboard")
        cards = ttk.Frame(tab)
        cards.pack(fill="x")
        self.dashboard_vars = {}
        labels = [
            ("Runtime", "runtime"), ("Transport", "transport"), ("Agents", "agents"),
            ("Tasks", "tasks"), ("Workflows", "workflows"), ("Nodes", "nodes"),
            ("RAG", "rag"), ("Outbox", "outbox"),
        ]
        for i, (title, key) in enumerate(labels):
            box = ttk.LabelFrame(cards, text=title, padding=10)
            box.grid(row=i // 4, column=i % 4, sticky="nsew", padx=5, pady=5)
            cards.grid_columnconfigure(i % 4, weight=1)
            var = tk.StringVar(value="-")
            self.dashboard_vars[key] = var
            ttk.Label(box, textvariable=var, justify="left").pack(anchor="w")
        detail = ttk.LabelFrame(tab, text="Runtime details", padding=8)
        detail.pack(fill="both", expand=True, pady=(8, 0))
        self.dashboard_text = ScrolledText(detail, height=18, wrap="word")
        self.dashboard_text.pack(fill="both", expand=True)
        self.dashboard_text.configure(state="disabled")

    def _build_agent_tab(self):
        tab = self._add_tab("Agent")
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(0, 6))
        ttk.Button(buttons, text="Stop selected invocation", command=self._stop_selected_invocation).pack(side="left", padx=3)
        ttk.Button(buttons, text="Stop all", command=lambda: self._action("agent.stop_all")).pack(side="left", padx=3)
        ttk.Button(buttons, text="Force config policy reload", command=lambda: self._action("config.reload")).pack(side="left", padx=3)
        pan = ttk.Panedwindow(tab, orient="vertical")
        pan.pack(fill="both", expand=True)
        upper = ttk.LabelFrame(pan, text="Active / waiting invocations", padding=4)
        self.invocation_tree = self._tree(upper, ("invocation_id", "state", "purpose", "conversation", "runtime", "detail"),
                                          {"invocation_id": 190, "state": 90, "purpose": 120, "conversation": 260, "runtime": 90, "detail": 360}, 8)
        pan.add(upper, weight=1)
        middle = ttk.LabelFrame(pan, text="Recent CodeAgent runs", padding=4)
        self.agent_run_tree = self._tree(middle, ("run_id", "status", "conversation", "pid", "elapsed", "last_output", "error"),
                                         {"run_id": 160, "status": 90, "conversation": 280, "pid": 70, "elapsed": 90, "last_output": 100, "error": 300}, 8)
        self.agent_run_tree.bind("<<TreeviewSelect>>", self._show_agent_tail)
        pan.add(middle, weight=1)
        lower = ttk.LabelFrame(pan, text="Selected run tail", padding=4)
        self.agent_tail = ScrolledText(lower, height=10, wrap="none")
        self.agent_tail.pack(fill="both", expand=True)
        pan.add(lower, weight=1)

    def _build_tasks_tab(self):
        tab = self._add_tab("Tasks")
        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(0, 6))
        ttk.Button(controls, text="Cancel", command=self._cancel_task).pack(side="left", padx=3)
        ttk.Button(controls, text="Retry", command=self._retry_task).pack(side="left", padx=3)
        ttk.Button(controls, text="Append instruction", command=lambda: self._task_instruction("append")).pack(side="left", padx=3)
        ttk.Button(controls, text="Steer", command=lambda: self._task_instruction("steer")).pack(side="left", padx=3)
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        self.task_tree = self._tree(holder, ("task_id", "state", "node", "title", "conversation", "updated"),
                                    {"task_id": 180, "state": 90, "node": 120, "title": 330, "conversation": 260, "updated": 130}, 22)

    def _build_workflows_tab(self):
        tab = self._add_tab("Workflows")
        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(0, 6))
        ttk.Button(controls, text="Cancel", command=self._cancel_workflow).pack(side="left", padx=3)
        ttk.Button(controls, text="Resume blocked", command=self._resume_workflow).pack(side="left", padx=3)
        ttk.Button(controls, text="Retry step…", command=self._retry_workflow_step).pack(side="left", padx=3)
        holder = ttk.Frame(tab)
        holder.pack(fill="both", expand=True)
        self.workflow_tree = self._tree(holder, ("workflow_id", "state", "summary", "conversation", "updated"),
                                        {"workflow_id": 180, "state": 100, "summary": 420, "conversation": 280, "updated": 130}, 22)

    def _build_nodes_peers_tab(self):
        tab = self._add_tab("Nodes & Peers")
        pan = ttk.Panedwindow(tab, orient="vertical")
        pan.pack(fill="both", expand=True)
        node_box = ttk.LabelFrame(pan, text="Execution nodes", padding=4)
        self.node_tree = self._tree(node_box, ("name", "online", "tasks", "capacity", "capabilities", "description"),
                                    {"name": 150, "online": 90, "tasks": 80, "capacity": 90, "capabilities": 300, "description": 330}, 10)
        pan.add(node_box, weight=1)
        peer_box = ttk.LabelFrame(pan, text="WorkBot peers (manual discovery + optional static pins)", padding=4)
        peer_controls = ttk.Frame(peer_box)
        peer_controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        ttk.Button(peer_controls, text="Discover WorkBots", command=lambda: self._action("collaboration.discover")).pack(side="left")
        self.peer_discovery_var = tk.StringVar(value="Discovered peers: 0 · Last discovery: never")
        ttk.Label(peer_controls, textvariable=self.peer_discovery_var).pack(side="left", padx=12)
        peer_holder = ttk.Frame(peer_box)
        peer_holder.grid(row=1, column=0, columnspan=2, sticky="nsew")
        peer_box.grid_rowconfigure(1, weight=1); peer_box.grid_columnconfigure(0, weight=1)
        self.peer_tree = self._tree(peer_holder, ("agent_id", "source", "sender", "name", "last_seen", "capabilities"),
                                    {"agent_id": 170, "source": 90, "sender": 160, "name": 180, "last_seen": 110, "capabilities": 350}, 10)
        pan.add(peer_box, weight=1)

    def _build_rag_tab(self):
        tab = self._add_tab("RAG")
        controls = ttk.Frame(tab)
        controls.pack(fill="x", pady=(0, 6))
        ttk.Button(controls, text="Sync", command=lambda: self._action("rag.sync.start", timeout=10)).pack(side="left", padx=3)
        ttk.Button(controls, text="Stop sync", command=lambda: self._action("rag.sync.stop")).pack(side="left", padx=3)
        ttk.Button(controls, text="Embed…", command=self._start_embed).pack(side="left", padx=3)
        ttk.Button(controls, text="Stop embed", command=lambda: self._action("rag.embed.stop")).pack(side="left", padx=3)
        self.rag_text = ScrolledText(tab, wrap="word")
        self.rag_text.pack(fill="both", expand=True)
        self.rag_text.configure(state="disabled")

    def _build_config_tab(self):
        tab = self._add_tab("Configuration")
        sub = ttk.Notebook(tab)
        sub.pack(fill="both", expand=True)
        general = ttk.Frame(sub, padding=10)
        raw = ttk.Frame(sub, padding=8)
        sub.add(general, text="Common settings")
        sub.add(raw, text="Raw JSON")

        self.config_vars = {
            "bot_reply_prefix": tk.StringVar(),
            "poll_interval": tk.StringVar(),
            "reload_interval": tk.StringVar(),
            "agent_command": tk.StringVar(),
            "agent_timeout": tk.StringVar(),
            "agent_max_concurrent": tk.StringVar(),
            "prompt_args": tk.StringVar(),
            "self_accounts": tk.StringVar(),
            "aliases": tk.StringVar(),
            "require_alias": tk.BooleanVar(),
            "collab_enabled": tk.BooleanVar(),
            "collab_agent_id": tk.StringVar(),
            "collab_groups": tk.StringVar(),
            "collab_discovery": tk.BooleanVar(value=True),
            "rag_enabled": tk.BooleanVar(),
        }
        fields = [
            ("Reply prefix", "bot_reply_prefix"), ("Poll interval (s)", "poll_interval"),
            ("Config reload interval (s)", "reload_interval"), ("CodeAgent command", "agent_command"),
            ("Agent timeout (s)", "agent_timeout"), ("Agent max concurrent", "agent_max_concurrent"),
            ("prompt_args (JSON array)", "prompt_args"), ("Self accounts (comma separated)", "self_accounts"),
            ("Bot aliases (comma separated)", "aliases"), ("Collaboration agent_id (blank = auto)", "collab_agent_id"),
            ("Collaboration groups (blank = configured IM groups)", "collab_groups"),
        ]
        for row, (label, key) in enumerate(fields):
            ttk.Label(general, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=4)
            ttk.Entry(general, textvariable=self.config_vars[key]).grid(row=row, column=1, sticky="ew", padx=5, pady=4)
        row = len(fields)
        checks = [
            ("Require alias", "require_alias"), ("Enable collaboration", "collab_enabled"),
            ("Enable manual peer discovery", "collab_discovery"), ("Enable RAG", "rag_enabled"),
        ]
        for i, (label, key) in enumerate(checks):
            ttk.Checkbutton(general, text=label, variable=self.config_vars[key]).grid(row=row + i // 2, column=i % 2, sticky="w", padx=5, pady=4)
        general.grid_columnconfigure(1, weight=1)
        btnrow = row + 3
        bar = ttk.Frame(general)
        bar.grid(row=btnrow, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(bar, text="Reload from disk", command=self._load_config_into_editor).pack(side="left", padx=3)
        ttk.Button(bar, text="Save common settings", command=self._save_common_config).pack(side="left", padx=3)
        ttk.Button(bar, text="Save & Restart", command=lambda: self._save_common_config(restart=True)).pack(side="left", padx=3)
        ttk.Label(general, text="Only ingestion_policy / reply_policy / execution_policy are hot-reloaded. Most common settings require Restart.", wraplength=850).grid(row=btnrow+1, column=0, columnspan=2, sticky="w", padx=5, pady=8)

        raw_bar = ttk.Frame(raw)
        raw_bar.pack(fill="x", pady=(0, 6))
        ttk.Button(raw_bar, text="Reload", command=self._load_config_into_editor).pack(side="left", padx=3)
        ttk.Button(raw_bar, text="Validate", command=self._validate_raw_config).pack(side="left", padx=3)
        ttk.Button(raw_bar, text="Save", command=self._save_raw_config).pack(side="left", padx=3)
        ttk.Button(raw_bar, text="Save & Restart", command=lambda: self._save_raw_config(restart=True)).pack(side="left", padx=3)
        self.config_text = ScrolledText(raw, wrap="none", undo=True, font=("Consolas", 10))
        self.config_text.pack(fill="both", expand=True)

    def _build_logs_tab(self):
        tab = self._add_tab("Logs")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 5))
        ttk.Button(bar, text="Clear view", command=lambda: self.log_text.delete("1.0", "end")).pack(side="left")
        self.auto_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto scroll", variable=self.auto_scroll).pack(side="left", padx=8)
        self.log_text = ScrolledText(tab, wrap="none", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    def _build_doctor_tab(self):
        tab = self._add_tab("Doctor")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 5))
        ttk.Button(bar, text="Run diagnostics", command=self._run_doctor).pack(side="left")
        self.doctor_text = ScrolledText(tab, wrap="word")
        self.doctor_text.pack(fill="both", expand=True)

    # ---------------------------------------------------------------- runtime
    def _start(self):
        try:
            self.controller.start()
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))

    def _stop(self):
        self.controller.stop()

    def _restart(self):
        self.controller.restart()

    def _action(self, name: str, timeout: float = 20.0, **kwargs):
        def work():
            try:
                result = self.controller.action(name, timeout=timeout, **kwargs)
                self.root.after(0, lambda: self._action_done(name, result))
            except Exception as exc:
                self.root.after(0, lambda e=exc: messagebox.showerror("Operation failed", str(e)))
        threading.Thread(target=work, name=f"gui-action:{name}", daemon=True).start()

    def _action_done(self, name, result):
        logging.getLogger(__name__).info("GUI action %s: %s", name, result)
        self._refresh_now()

    def _selected_value(self, tree, column: str):
        sel = tree.selection()
        if not sel:
            return ""
        return str(tree.set(sel[0], column))

    def _stop_selected_invocation(self):
        invocation_id = self._selected_value(self.invocation_tree, "invocation_id")
        if invocation_id:
            self._action("agent.stop", invocation_id=invocation_id)

    def _cancel_task(self):
        task_id = self._selected_value(self.task_tree, "task_id")
        if task_id and messagebox.askyesno("Cancel task", f"Cancel {task_id}?"):
            self._action("task.cancel", task_id=task_id)

    def _retry_task(self):
        task_id = self._selected_value(self.task_tree, "task_id")
        if task_id:
            self._action("task.retry", task_id=task_id)

    def _task_instruction(self, mode: str):
        task_id = self._selected_value(self.task_tree, "task_id")
        if not task_id:
            return
        value = simpledialog.askstring("Task instruction", f"{mode} instruction for {task_id}:", parent=self.root)
        if value and value.strip():
            self._action(f"task.{mode}", task_id=task_id, instruction=value.strip())

    def _cancel_workflow(self):
        wid = self._selected_value(self.workflow_tree, "workflow_id")
        if wid and messagebox.askyesno("Cancel workflow", f"Cancel {wid}?"):
            self._action("workflow.cancel", workflow_id=wid)

    def _resume_workflow(self):
        wid = self._selected_value(self.workflow_tree, "workflow_id")
        if wid:
            self._action("workflow.resume", workflow_id=wid)

    def _retry_workflow_step(self):
        wid = self._selected_value(self.workflow_tree, "workflow_id")
        if not wid:
            return
        sid = simpledialog.askstring("Retry step", f"Step ID in {wid}:", parent=self.root)
        if sid and sid.strip():
            self._action("workflow.retry_step", workflow_id=wid, step_id=sid.strip())

    def _start_embed(self):
        value = simpledialog.askstring("RAG embed", "Chunk limit (number) or 'all':", initialvalue="100", parent=self.root)
        if not value:
            return
        if value.strip().lower() == "all":
            self._action("rag.embed.start", timeout=10, all_chunks=True)
            return
        try:
            limit = max(1, int(value))
        except ValueError:
            messagebox.showerror("Invalid limit", "Enter a positive integer or 'all'.")
            return
        self._action("rag.embed.start", timeout=10, limit=limit)

    # ---------------------------------------------------------------- config
    def _load_config_into_editor(self):
        try:
            cfg = load_config(self.config_path)
        except Exception as exc:
            messagebox.showerror("Config", str(exc)); return
        self.config_text.delete("1.0", "end")
        self.config_text.insert("1.0", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        v = self.config_vars
        v["bot_reply_prefix"].set(str(cfg.get("bot_reply_prefix", "[AGENT]")))
        v["poll_interval"].set(str(cfg.get("poll_interval_seconds", 3)))
        v["reload_interval"].set(str(cfg.get("config_reload_interval_seconds", 2)))
        v["agent_command"].set(str(_nested(cfg, "agent", "command", default="codeagent")))
        v["agent_timeout"].set(str(_nested(cfg, "agent", "timeout_seconds", default=900)))
        v["agent_max_concurrent"].set(str(_nested(cfg, "agent", "max_concurrent", default=2)))
        v["prompt_args"].set(json.dumps(_nested(cfg, "agent", "prompt_args", default=[]), ensure_ascii=False))
        v["self_accounts"].set(", ".join(_nested(cfg, "im", "self_accounts", default=[]) or []))
        v["aliases"].set(", ".join(_nested(cfg, "im", "intent", "bot_aliases", default=[]) or []))
        v["require_alias"].set(bool(_nested(cfg, "im", "intent", "require_alias", default=False)))
        v["collab_enabled"].set(bool(_nested(cfg, "collaboration", "enabled", default=False)))
        v["collab_agent_id"].set(str(_nested(cfg, "collaboration", "agent_id", default="")))
        v["collab_groups"].set(", ".join(_nested(cfg, "collaboration", "groups", default=[]) or []))
        legacy_discovery = _nested(cfg, "collaboration", "auto_discovery", default=True)
        v["collab_discovery"].set(bool(_nested(cfg, "collaboration", "discovery_enabled", default=legacy_discovery)))
        v["rag_enabled"].set(bool(_nested(cfg, "rag", "enabled", default=False)))

    def _config_from_common(self) -> dict:
        cfg = load_config(self.config_path)
        v = self.config_vars
        cfg["bot_reply_prefix"] = v["bot_reply_prefix"].get().strip() or "[AGENT]"
        cfg["poll_interval_seconds"] = float(v["poll_interval"].get())
        cfg["config_reload_interval_seconds"] = float(v["reload_interval"].get())
        agent = cfg.setdefault("agent", {})
        agent["command"] = v["agent_command"].get().strip() or "codeagent"
        agent["timeout_seconds"] = int(v["agent_timeout"].get())
        agent["max_concurrent"] = int(v["agent_max_concurrent"].get())
        prompt_args = json.loads(v["prompt_args"].get() or "[]")
        if not isinstance(prompt_args, list):
            raise ValueError("prompt_args must be a JSON array")
        agent["prompt_args"] = prompt_args
        im = cfg.setdefault("im", {})
        im["self_accounts"] = _csv(v["self_accounts"].get())
        intent = im.setdefault("intent", {})
        intent["bot_aliases"] = _csv(v["aliases"].get())
        intent["require_alias"] = bool(v["require_alias"].get())
        collab = cfg.setdefault("collaboration", {})
        collab["enabled"] = bool(v["collab_enabled"].get())
        collab["agent_id"] = v["collab_agent_id"].get().strip()
        collab["groups"] = _csv(v["collab_groups"].get())
        collab["discovery_enabled"] = bool(v["collab_discovery"].get())
        collab.pop("auto_discovery", None)
        collab.pop("announce_interval_seconds", None)
        collab.setdefault("peers", {})
        cfg.setdefault("rag", {})["enabled"] = bool(v["rag_enabled"].get())
        return cfg

    def _save_config_obj(self, cfg: dict, *, restart: bool):
        errors = validate_config(cfg)
        if errors:
            raise ValueError("\n".join(errors))
        keep = int((cfg.get("setup") or {}).get("config_backup_count", 5))
        backup_config(self.config_path, keep=keep)
        save_config_atomic(self.config_path, cfg)
        template = self.workspace / "AGENTS.example.md"
        if template.exists():
            generate_agents_file(template, cfg, self.workspace / "AGENTS.md", backup_manual=True)
        if restart:
            self.controller.restart()
        elif self.controller.state == "running":
            # Apply supported policies immediately; all other edits stay marked
            # restart-bound and will be picked up when the operator restarts.
            self._action("config.reload")

    def _save_common_config(self, restart: bool = False):
        try:
            cfg = self._config_from_common()
            self._save_config_obj(cfg, restart=restart)
            self._load_config_into_editor()
            messagebox.showinfo("Configuration", "Saved." + (" WorkBot is restarting." if restart else " Restart required for non-policy runtime settings."))
        except Exception as exc:
            messagebox.showerror("Configuration", str(exc))

    def _raw_config_obj(self):
        value = self.config_text.get("1.0", "end").strip()
        cfg = json.loads(value)
        if not isinstance(cfg, dict):
            raise ValueError("Config root must be a JSON object")
        return cfg

    def _validate_raw_config(self):
        try:
            cfg = self._raw_config_obj()
            errors = validate_config(cfg)
            if errors:
                messagebox.showerror("Validation", "\n".join(errors))
            else:
                messagebox.showinfo("Validation", "Configuration is valid.")
        except Exception as exc:
            messagebox.showerror("Validation", str(exc))

    def _save_raw_config(self, restart: bool = False):
        try:
            cfg = self._raw_config_obj()
            self._save_config_obj(cfg, restart=restart)
            self._load_config_into_editor()
            messagebox.showinfo("Configuration", "Saved." + (" WorkBot is restarting." if restart else ""))
        except Exception as exc:
            messagebox.showerror("Configuration", str(exc))

    # ---------------------------------------------------------------- doctor
    def _run_doctor(self):
        self.doctor_text.delete("1.0", "end")
        self.doctor_text.insert("1.0", "Running diagnostics…\n")
        def work():
            try:
                result = run_doctor(self.config_path, workspace=self.workspace)
                lines = ["WorkBot Doctor", ""]
                for c in result.get("checks", []):
                    mark = "-" if c.get("skipped") else ("✓" if c.get("ok") else "✗")
                    lines.append(f"{mark} {c.get('name')}: {c.get('detail')}")
                lines += ["", f"exit_code={result.get('exit_code')} ok={result.get('ok')}"]
                text = "\n".join(lines)
            except Exception as exc:
                text = f"Doctor failed: {exc}"
            self.root.after(0, lambda: self._set_text(self.doctor_text, text))
        threading.Thread(target=work, name="workbot-doctor", daemon=True).start()

    # ---------------------------------------------------------------- refresh
    def _refresh_now(self):
        if self._snapshot_busy:
            return
        self._snapshot_busy = True
        def work():
            try:
                snap = self.controller.snapshot()
            except Exception as exc:
                snap = {"controller": {"state": self.controller.state, "error": str(exc)}}
            self.root.after(0, lambda: self._apply_snapshot(snap))
        threading.Thread(target=work, name="workbot-gui-refresh", daemon=True).start()

    def _schedule_refresh(self):
        if self._closing:
            return
        self._refresh_now()
        self._append_logs()
        self.root.after(1000, self._schedule_refresh)

    def _apply_snapshot(self, snap: dict):
        self._snapshot_busy = False
        self.snapshot = snap
        ctl = snap.get("controller", {})
        state = str(ctl.get("state", "unknown")).upper()
        self.status_var.set(state + ((": " + str(ctl.get("error"))) if ctl.get("error") else ""))
        uptime = int(float(ctl.get("uptime") or 0))
        self.uptime_var.set(f"uptime {uptime}s" if uptime else "")
        self._refresh_dashboard(snap)
        self._refresh_agents(snap)
        self._refresh_tasks(snap)
        self._refresh_workflows(snap)
        self._refresh_nodes_peers(snap)
        self._refresh_rag(snap)

    def _refresh_dashboard(self, s):
        state = _nested(s, "controller", "state", default="stopped")
        transport = s.get("transport") or {}
        scheduler = s.get("scheduler") or {}
        tasks = s.get("tasks") or []
        workflows = s.get("workflows") or []
        nodes = s.get("nodes") or []
        rag = s.get("rag") or {}
        outbox = s.get("outbox") or {}
        self.dashboard_vars["runtime"].set(str(state))
        self.dashboard_vars["transport"].set(f"{transport.get('mode','-')}\nrealtime={transport.get('realtime_connected', False)}")
        self.dashboard_vars["agents"].set(f"active={scheduler.get('active',0)} / {scheduler.get('max_concurrent',0)}\nwaiting={scheduler.get('waiting',0)} stopping={scheduler.get('stopping',0)}")
        self.dashboard_vars["tasks"].set(f"active={sum(1 for x in tasks if x.get('state') in {'created','running','cancelling'})}\ntotal shown={len(tasks)}")
        self.dashboard_vars["workflows"].set(f"active={sum(1 for x in workflows if x.get('state') in {'created','running','recovering','blocked','cancelling'})}\ntotal shown={len(workflows)}")
        self.dashboard_vars["nodes"].set(f"online={sum(1 for x in nodes if x.get('online'))}/{len(nodes)}")
        self.dashboard_vars["rag"].set(f"enabled={rag.get('enabled',False)}\ncoverage={rag.get('coverage_percent','-')}%")
        self.dashboard_vars["outbox"].set(f"pending={outbox.get('pending',0)}\nsending={outbox.get('sending',0)}")
        collab = s.get("collaboration") or {}
        details = {
            "controller": s.get("controller"), "runtime": s.get("runtime"), "transport": transport,
            "scheduler": scheduler, "collaboration": collab,
        }
        self._set_text(self.dashboard_text, json.dumps(details, ensure_ascii=False, indent=2))

    def _clear_tree(self, tree):
        for item in tree.get_children():
            tree.delete(item)

    def _refresh_agents(self, s):
        self._clear_tree(self.invocation_tree)
        for row in s.get("invocations") or []:
            runtime = row.get("run_seconds") if row.get("state") in {"active", "stopping"} else row.get("wait_seconds")
            self.invocation_tree.insert("", "end", values=(row.get("invocation_id"), row.get("state"), row.get("purpose"), row.get("conversation_id"), f"{float(runtime or 0):.1f}s", row.get("detail") or row.get("task_name") or ""))
        selected_run = self._selected_value(self.agent_run_tree, "run_id")
        self._clear_tree(self.agent_run_tree)
        for row in s.get("agent_runs") or []:
            age = row.get("last_output_age")
            last = "-" if age is None else f"{int(age)}s ago"
            iid = self.agent_run_tree.insert("", "end", values=(row.get("run_id"), row.get("status"), row.get("conversation_id"), row.get("pid") or "", f"{float(row.get('elapsed') or 0):.1f}s", last, row.get("error") or ""))
            if row.get("run_id") == selected_run:
                self.agent_run_tree.selection_set(iid)
        self._show_agent_tail()

    def _show_agent_tail(self, _event=None):
        run_id = self._selected_value(self.agent_run_tree, "run_id")
        if not run_id:
            return
        for row in self.snapshot.get("agent_runs") or []:
            if row.get("run_id") == run_id:
                self._set_text(self.agent_tail, "\n".join(row.get("recent_output") or []))
                return

    def _refresh_tasks(self, s):
        self._clear_tree(self.task_tree)
        for row in s.get("tasks") or []:
            self.task_tree.insert("", "end", values=(row.get("task_id"), row.get("state"), row.get("node"), row.get("title") or "", row.get("origin_conversation_id") or "", row.get("updated_at") or ""))

    def _refresh_workflows(self, s):
        self._clear_tree(self.workflow_tree)
        for row in s.get("workflows") or []:
            self.workflow_tree.insert("", "end", values=(row.get("workflow_id"), row.get("state"), row.get("summary") or "", row.get("origin_conversation_id") or "", row.get("updated_at") or ""))

    def _refresh_nodes_peers(self, s):
        self._clear_tree(self.node_tree)
        for row in s.get("nodes") or []:
            self.node_tree.insert("", "end", values=(row.get("name"), "yes" if row.get("online") else "no", row.get("active_tasks"), row.get("max_concurrent"), ",".join(row.get("capabilities") or []), row.get("description") or ""))
        self._clear_tree(self.peer_tree)
        now = time.time()
        peers = _nested(s, "collaboration", "peers", default=[]) or []
        last_discovery = _nested(s, "collaboration", "last_discovery_at", default=None)
        discovery_text = "never" if not last_discovery else time.strftime("%H:%M:%S", time.localtime(float(last_discovery)))
        self.peer_discovery_var.set(f"Discovered peers: {len(peers)} · Last discovery: {discovery_text}")
        for row in peers:
            last = float(row.get("last_seen") or 0)
            seen = "-" if not last else f"{int(max(0, now-last))}s ago"
            self.peer_tree.insert("", "end", values=(row.get("agent_id"), row.get("source"), ",".join(row.get("sender_accounts") or []), row.get("display_name") or ",".join(row.get("aliases") or []), seen, ",".join(row.get("capabilities") or [])))

    def _refresh_rag(self, s):
        rag = s.get("rag") or {}
        self._set_text(self.rag_text, json.dumps(rag, ensure_ascii=False, indent=2, default=str))

    def _append_logs(self):
        seq, lines = self.controller.log_buffer.read_since(self._last_log_seq, limit=1200)
        self._last_log_seq = seq
        if lines:
            self.log_text.insert("end", "\n".join(lines) + "\n")
            if self.auto_scroll.get():
                self.log_text.see("end")

    @staticmethod
    def _set_text(widget: ScrolledText, text: str):
        state = str(widget.cget("state"))
        if state == "disabled":
            widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", str(text))
        if state == "disabled":
            widget.configure(state="disabled")

    def _on_close(self):
        self._closing = True
        if self.controller.state in {"running", "starting", "stopping"}:
            if not messagebox.askyesno("Exit", "Stop WorkBot and close the Control Center?"):
                self._closing = False
                return
            self.controller.stop()
        self.root.destroy()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="workbot-gui")
    ap.add_argument("--config", default="config/local.json")
    ap.add_argument("--autostart", action="store_true", help="start WorkBot runtime when the window opens")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = tk.Tk()
    app = WorkBotGUI(root, args.config)
    if args.autostart:
        root.after(150, app._start)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
