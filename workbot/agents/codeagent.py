from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
import sys
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path

UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


@dataclass(slots=True)
class AgentResult:
    text: str
    session_id: str | None = None
    returncode: int = 0
    action: dict | None = None
    approval: dict | None = None


@dataclass(slots=True)
class AgentRunState:
    run_id: str
    conversation_id: str = ""
    session_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    pid: int | None = None
    status: str = "starting"
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    last_output_at: float | None = None
    last_activity_at: float | None = None
    recent_output: deque[str] = field(default_factory=deque)
    exit_code: int | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float:
        return max(0.0, (self.ended_at or time.time()) - self.started_at)


class CodeAgentBackend:
    """Thin adapter around the company's ``codeagent`` CLI.

    Observed non-interactive invocation::

        codeagent -p "..." --skip-safe-check

    Windows note
    ------------
    The installed command may resolve to ``codeagent.bat``. Passing a large,
    multiline prompt directly as a ``-p`` argument through a batch shim is not
    reliable: cmd/batch parsing may lose later lines or hit the much smaller
    cmd.exe command-line limit. WorkBot therefore materializes the *actual*
    prompt into a temporary UTF-8 file for Windows ``.bat``/``.cmd`` shims and
    passes CodeAgent a short one-line bootstrap instruction telling it to read
    that file first. This also keeps the user's latest instruction intact even
    when the planner prompt contains workspace/context material.
    """

    def __init__(self, cfg: dict, workspace: Path):
        configured = cfg.get("command", "codeagent")
        self.command = os.environ.get("WORKBOT_CODEAGENT_COMMAND") or configured
        self.workspace = workspace.resolve()
        self.cwd = (workspace / cfg.get("cwd", ".")).resolve()
        self.timeout = int(cfg.get("timeout_seconds", 900))
        self.new_args = list(cfg.get("new_args", []))
        self.new_session_args = list(cfg.get("new_session_args", ["--session-id", "{session_id}"]))
        self.resume_args = list(cfg.get("resume_args", ["--sessions", "{session_id}"]))
        self.prompt_transport = cfg.get("prompt_transport", "arg")
        self.prompt_args = list(cfg.get("prompt_args", ["--skip-safe-check", "--permission-mode", "bypassPermissions", "-p", "{prompt}"]))
        self.windows_batch_prompt_file = bool(cfg.get("windows_batch_prompt_file", True))
        self._resolved_command: str | None = None
        self._managed_welink_enabled = False
        self._managed_welink_real_cli = ""
        self._managed_welink_gate_path = ""
        self._managed_welink_rate_path = ""
        self._managed_welink_trace = False
        obs = dict(cfg.get("observability", {}) or {})
        self.recent_output_lines = max(1, int(obs.get("recent_output_lines", 50)))
        self.recent_output_max_chars = max(256, int(obs.get("recent_output_max_chars", 16384)))
        self.persist_run_summary = bool(obs.get("persist_run_summary", True))
        self._run_states: dict[str, AgentRunState] = {}
        self._run_order: deque[str] = deque(maxlen=200)


    def _new_run_state(self, *, conversation_id: str = "", session_id: str | None = None,
                       task_id: str | None = None, workflow_id: str | None = None) -> AgentRunState:
        state = AgentRunState(
            run_id=f"arun-{uuid.uuid4().hex[:12]}", conversation_id=str(conversation_id or ""),
            session_id=session_id, task_id=task_id, workflow_id=workflow_id,
            recent_output=deque(maxlen=self.recent_output_lines),
        )
        self._run_states[state.run_id] = state
        self._run_order.append(state.run_id)
        return state

    def _append_output(self, state: AgentRunState, stream: str, text: str) -> None:
        line = text.rstrip("\r\n")
        if not line:
            return
        state.recent_output.append(f"[{stream}] {line}")
        while sum(len(x) + 1 for x in state.recent_output) > self.recent_output_max_chars and len(state.recent_output) > 1:
            state.recent_output.popleft()
        now = time.time()
        state.last_output_at = now
        state.last_activity_at = now

    def run_state(self, run_id: str) -> AgentRunState | None:
        return self._run_states.get(str(run_id))

    def latest_run(self, conversation_id: str = "") -> AgentRunState | None:
        for run_id in reversed(self._run_order):
            state = self._run_states.get(run_id)
            if state and (not conversation_id or state.conversation_id == conversation_id):
                return state
        return None

    def list_runs(self, conversation_id: str = "") -> list[AgentRunState]:
        out = []
        for run_id in reversed(self._run_order):
            state = self._run_states.get(run_id)
            if state and (not conversation_id or state.conversation_id == conversation_id):
                out.append(state)
        return out

    async def _stream_reader(self, stream: asyncio.StreamReader | None, state: AgentRunState, name: str, collector: list[bytes]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            collector.append(chunk)
            self._append_output(state, name, chunk.decode("utf-8", errors="replace"))


    def configure_managed_welink(self, *, real_cli: str, gate_path: str, rate_path: str, trace: bool = False) -> None:
        self._managed_welink_enabled = bool(real_cli)
        self._managed_welink_real_cli = str(real_cli or "")
        self._managed_welink_gate_path = str(gate_path or "")
        self._managed_welink_rate_path = str(rate_path or "")
        self._managed_welink_trace = bool(trace)

    def _subprocess_env(self, *, tool_mode: str = "disabled", approval_token: str | None = None) -> dict[str, str]:
        env = os.environ.copy()
        if self._managed_welink_enabled:
            tool_bin = self.workspace / "scripts" / "tool-bin"
            env["PATH"] = str(tool_bin) + os.pathsep + env.get("PATH", "")
            env["WORKBOT_ROOT"] = str(self.workspace)
            env["WORKBOT_PYTHON"] = sys.executable
            env["WORKBOT_REAL_WELINK_CLI"] = self._managed_welink_real_cli
            env["WORKBOT_WELINK_GATE_PATH"] = self._managed_welink_gate_path
            env["WORKBOT_WELINK_RATE_PATH"] = self._managed_welink_rate_path
            env["WORKBOT_WELINK_MODE"] = str(tool_mode or "disabled")
            env["WORKBOT_WELINK_GATEWAY_TRACE"] = "1" if self._managed_welink_trace else "0"
            if approval_token:
                env["WORKBOT_WELINK_APPROVAL_TOKEN"] = str(approval_token)
            else:
                env.pop("WORKBOT_WELINK_APPROVAL_TOKEN", None)
        return env

    def _resolve_command(self) -> str:
        if self._resolved_command:
            return self._resolved_command

        raw = os.path.expandvars(os.path.expanduser(str(self.command)))
        path = Path(raw)
        if path.is_absolute() or path.parent != Path("."):
            if path.exists():
                resolved = str(path.resolve())
            else:
                resolved = shutil.which(raw) or ""
        else:
            resolved = shutil.which(raw) or ""

        if not resolved:
            env_hint = os.environ.get("WORKBOT_CODEAGENT_COMMAND")
            raise FileNotFoundError(
                "找不到 codeagent 可执行文件。"
                f" configured={self.command!r}, WORKBOT_CODEAGENT_COMMAND={env_hint!r}. "
                "请在启动 WorkBot 的同一 PowerShell 中运行 `Get-Command codeagent | Format-List Source,Path,CommandType`。"
            )
        self._resolved_command = resolved
        return resolved

    def _launcher(self, agent_args: list[str]) -> list[str]:
        resolved = self._resolve_command()
        suffix = Path(resolved).suffix.lower()
        if os.name == "nt" and suffix == ".ps1":
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if not powershell:
                raise FileNotFoundError("codeagent resolves to a .ps1 script, but powershell.exe was not found")
            return [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                resolved,
                *agent_args,
            ]
        return [resolved, *agent_args]

    def _needs_prompt_file(self, resolved_command: str, prompt: str) -> bool:
        if not self.windows_batch_prompt_file or self.prompt_transport != "arg":
            return False
        suffix = Path(resolved_command).suffix.lower()
        # Apply on Windows batch shims. Tests may call this helper directly on
        # non-Windows hosts, so the suffix check is intentionally separated.
        return os.name == "nt" and suffix in {".bat", ".cmd"} and ("\n" in prompt or "\r" in prompt or len(prompt) > 3000)

    def _write_prompt_file(self, prompt: str) -> Path:
        prompt_dir = self.workspace / "state" / "runtime-prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        # Prompt files may contain conversation/task context. Normally they are
        # deleted immediately after CodeAgent exits. Clean up stale files left
        # by a crashed WorkBot process so sensitive context does not accumulate.
        cutoff = time.time() - 24 * 3600
        for stale in prompt_dir.glob("prompt-*.txt"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink(missing_ok=True)
            except OSError:
                pass
        path = prompt_dir / f"prompt-{uuid.uuid4().hex}.txt"
        path.write_text(prompt, encoding="utf-8", newline="\n")
        return path

    @staticmethod
    def _bootstrap_for_prompt_file(path: Path) -> str:
        # Keep this physically one line so it remains safe through a .bat shim.
        return (
            "IMPORTANT: the actual WorkBot instruction for this invocation is in the UTF-8 text file "
            f"{path}. Read that file completely FIRST, then follow it exactly. "
            "Do not answer based only on AGENTS.md or prior context, and do not ask what the task is before reading the file."
        )


    async def _terminate_process_tree(self, proc: asyncio.subprocess.Process, *, timeout: float = 8.0) -> None:
        """Best-effort termination of the whole CodeAgent process tree.

        On Windows the configured command is commonly a .bat/.cmd shim. Killing
        only the immediate cmd.exe can leave the real CodeAgent child alive with
        inherited stdout/stderr handles; ``proc.wait()`` then appears to hang for
        minutes. ``taskkill /T /F`` terminates the full descendant tree. POSIX
        launches use a new session and kill the corresponding process group.
        """
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
                if taskkill and proc.pid:
                    killer = await asyncio.create_subprocess_exec(
                        taskkill, "/PID", str(proc.pid), "/T", "/F",
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(killer.wait(), timeout=min(timeout, 5.0))
                    except asyncio.TimeoutError:
                        killer.kill()
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                # Do not let operator stop hold a scheduler slot indefinitely.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            # Cancellation/timeout cleanup is best-effort; never mask the
            # original invocation result with a process-reaping failure.
            pass

    async def _cleanup_after_cancel(self, proc: asyncio.subprocess.Process) -> None:
        """Run process-tree cleanup even if /stop-session is issued repeatedly."""
        cleanup = asyncio.create_task(
            self._terminate_process_tree(proc),
            name=f"codeagent-reap:{getattr(proc, 'pid', 0)}",
        )
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # A repeated operator stop used to cancel this cleanup await and
                # strand the subprocess. Consume only the *extra* cancellation
                # while reaping; the caller re-raises the original cancellation.
                current = asyncio.current_task()
                if current is not None and hasattr(current, "uncancel"):
                    current.uncancel()
                continue
        try:
            cleanup.result()
        except Exception:
            pass

    async def run(self, prompt: str, session_id: str | None = None, *, new_session_id: str | None = None, tool_mode: str = "disabled", approval_token: str | None = None, conversation_id: str = "", task_id: str | None = None, workflow_id: str | None = None) -> AgentResult:
        """Run CodeAgent.

        ``session_id`` resumes an existing conversation via ``--sessions``.
        ``new_session_id`` creates a new conversation with a WorkBot-assigned
        UUID via ``--session-id``.  Keeping these two modes explicit avoids
        depending on the CLI to print a newly-created session UUID.
        """
        if session_id and new_session_id:
            raise ValueError("session_id and new_session_id are mutually exclusive")
        agent_args: list[str] = []
        effective_session_id = session_id or new_session_id
        if session_id:
            agent_args += [x.format(session_id=session_id) for x in self.resume_args]
        else:
            if new_session_id:
                agent_args += [x.format(session_id=new_session_id) for x in self.new_session_args]
            agent_args += self.new_args

        resolved = self._resolve_command()
        prompt_file: Path | None = None
        prompt_for_cli = prompt
        if self._needs_prompt_file(resolved, prompt):
            prompt_file = self._write_prompt_file(prompt)
            prompt_for_cli = self._bootstrap_for_prompt_file(prompt_file)

        stdin_data: bytes | None = None
        if self.prompt_transport == "arg":
            agent_args += [x.format(prompt=prompt_for_cli, session_id=effective_session_id or "") for x in self.prompt_args]
            stdin_mode = asyncio.subprocess.DEVNULL
        elif self.prompt_transport == "stdin":
            stdin_mode = asyncio.subprocess.PIPE
            stdin_data = prompt.encode("utf-8")
        else:
            raise ValueError(f"unsupported codeagent prompt_transport={self.prompt_transport!r}")

        args = self._launcher(agent_args)
        proc_kwargs = dict(
            cwd=str(self.cwd),
            stdin=stdin_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(tool_mode=tool_mode, approval_token=approval_token),
        )
        if os.name == "nt":
            proc_kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            proc_kwargs["start_new_session"] = True
        state = self._new_run_state(
            conversation_id=conversation_id, session_id=effective_session_id, task_id=task_id, workflow_id=workflow_id
        )
        proc = await asyncio.create_subprocess_exec(*args, **proc_kwargs)
        state.pid = proc.pid
        state.status = "running"
        state.last_activity_at = time.time()
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_reader = asyncio.create_task(self._stream_reader(proc.stdout, state, "stdout", stdout_chunks), name=f"{state.run_id}:stdout")
        stderr_reader = asyncio.create_task(self._stream_reader(proc.stderr, state, "stderr", stderr_chunks), name=f"{state.run_id}:stderr")
        try:
            if stdin_data is not None and proc.stdin is not None:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.timeout)
                state.status = "finishing"
                await asyncio.gather(stdout_reader, stderr_reader)
            except asyncio.TimeoutError:
                state.status = "timed_out"
                state.error = f"codeagent timed out after {self.timeout}s"
                state.ended_at = time.time()
                await self._terminate_process_tree(proc)
                await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
                tail = "\n".join(state.recent_output)
                age = int(time.time() - state.last_output_at) if state.last_output_at else None
                raise RuntimeError(
                    f"codeagent timed out after {self.timeout}s; run_id={state.run_id}; pid={state.pid}; "
                    f"last_output_age={age if age is not None else 'n/a'}s; recent_output={tail[-2000:]}"
                )
            except asyncio.CancelledError:
                state.status = "cancelled"
                state.ended_at = time.time()
                if proc.returncode is None:
                    await self._cleanup_after_cancel(proc)
                await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
                raise
        finally:
            if prompt_file is not None:
                try:
                    prompt_file.unlink(missing_ok=True)
                except OSError:
                    pass

        out = b"".join(stdout_chunks)
        err = b"".join(stderr_chunks)
        stdout = out.decode("utf-8", errors="replace").strip()
        stderr = err.decode("utf-8", errors="replace").strip()
        text = stdout or stderr
        state.exit_code = proc.returncode
        state.ended_at = time.time()
        state.last_activity_at = state.ended_at
        if proc.returncode != 0:
            state.status = "failed"
            state.error = f"codeagent exited {proc.returncode}"
            raise RuntimeError(f"codeagent exited {proc.returncode}: {text[-2000:]}")
        state.status = "completed"
        found = UUID_RE.findall(stdout + "\n" + stderr)
        discovered = found[-1] if found else effective_session_id
        state.session_id = discovered
        return AgentResult(text=text, session_id=discovered, returncode=proc.returncode)
