import asyncio
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_agentctl():
    name = f"workbot_test_agentctl_v09_{os.urandom(4).hex()}"
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "linux" / "bin" / "agentctl"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_worker_recovery_reattaches_running_tmux(tmp_path: Path):
    import sys
    sys.path.insert(0, str(ROOT / "linux"))
    from workbot_worker.daemon import WorkerDaemon

    d = WorkerDaemon(tmp_path)
    task_dir = tmp_path / "tasks" / "task-recover1"
    task_dir.mkdir(parents=True)
    d.store.execute(
        "INSERT INTO tasks(task_id,task_type,title,instruction,state,session_id,workdir) VALUES (?,?,?,?,?,?,?)",
        ("task-recover1", "codeagent", "x", "hello", "running", "workbot-task-recover1", str(task_dir)),
    )

    async def exists(_session):
        return True

    seen = asyncio.Event()
    async def monitor(task_id, session, task_dir_arg):
        assert task_id == "task-recover1"
        assert session == "workbot-task-recover1"
        assert task_dir_arg == task_dir
        seen.set()

    d._tmux_session_exists = exists
    d._monitor_task = monitor
    await d._recover_tasks()
    await asyncio.wait_for(seen.wait(), timeout=1)
    row = d.store.one("SELECT * FROM tasks WHERE task_id='task-recover1'")
    assert row["state"] == "running"
    assert row["recovery_count"] == 1
    assert row["last_recovered_at"] is not None


@pytest.mark.asyncio
async def test_worker_recovery_finalizes_finished_task_once(tmp_path: Path):
    import sys
    sys.path.insert(0, str(ROOT / "linux"))
    from workbot_worker.daemon import WorkerDaemon

    d = WorkerDaemon(tmp_path)
    task_id = "task-finished1"
    task_dir = tmp_path / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "exit_code").write_text("0\n", encoding="utf-8")
    (task_dir / "output.log").write_text("finished after worker stopped", encoding="utf-8")
    d.store.execute(
        "INSERT INTO tasks(task_id,task_type,title,instruction,state,session_id,workdir) VALUES (?,?,?,?,?,?,?)",
        (task_id, "codeagent", "x", "hello", "running", "workbot-finished", str(task_dir)),
    )
    await d._recover_tasks()
    for _ in range(30):
        row = d.store.one("SELECT state FROM tasks WHERE task_id=?", (task_id,))
        if row["state"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert d.store.one("SELECT state FROM tasks WHERE task_id=?", (task_id,))["state"] == "completed"
    pending = d.store.pending()
    ids = [r["event_id"] for r in pending]
    assert ids.count(f"evt-{task_id}-completed") == 1
    # Re-running recovery does not create a second terminal event.
    await d._recover_tasks()
    ids2 = [r["event_id"] for r in d.store.pending()]
    assert ids2.count(f"evt-{task_id}-completed") == 1


@pytest.mark.asyncio
async def test_worker_recovery_marks_lost_running_task_failed(tmp_path: Path):
    import sys
    sys.path.insert(0, str(ROOT / "linux"))
    from workbot_worker.daemon import WorkerDaemon

    d = WorkerDaemon(tmp_path)
    task_id = "task-lost1"
    d.store.execute(
        "INSERT INTO tasks(task_id,task_type,title,instruction,state,session_id,workdir) VALUES (?,?,?,?,?,?,?)",
        (task_id, "codeagent", "x", "hello", "running", "workbot-lost", str(tmp_path / "tasks" / task_id)),
    )
    async def missing(_session):
        return False
    d._tmux_session_exists = missing
    await d._recover_tasks()
    row = d.store.one("SELECT state,result_json,recovery_count FROM tasks WHERE task_id=?", (task_id,))
    assert row["state"] == "failed"
    assert row["recovery_count"] == 1
    assert json.loads(row["result_json"])["error_type"] == "WorkerRecoveryLostTask"
    assert any(r["event_id"] == f"evt-{task_id}-failed" for r in d.store.pending())


def test_gated_execute_binds_exact_argv_and_audits(monkeypatch, tmp_path: Path):
    mod = load_agentctl()
    calls = []
    def fake_send(obj, timeout=0):
        calls.append(obj)
        if obj.get("type") == "request":
            return {"data": {"ok": True, "result": {"approved": True, "approval_id": "approval-1", "auto": False}}}
        return {"type": "ack", "data": {"persisted": True}}
    ran = []
    def fake_run(argv):
        ran.append(argv)
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(mod, "send", fake_send)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    rc = mod.gated_execute("git.push", "push main", ["/usr/bin/git", "push", "origin", "main"], details={"note": "x"})
    assert rc == 7
    assert ran == [["/usr/bin/git", "push", "origin", "main"]]
    req = calls[0]
    assert req["method"] == "policy.request_approval"
    assert req["data"]["details"]["argv"] == ["/usr/bin/git", "push", "origin", "main"]
    assert req["data"]["details"]["cwd"] == str(tmp_path)
    assert calls[1]["event"] == "policy.action.executed"
    assert calls[1]["data"]["approval_id"] == "approval-1"
    assert calls[1]["data"]["exit_code"] == 7


def test_gated_execute_denied_never_runs(monkeypatch):
    mod = load_agentctl()
    monkeypatch.setattr(mod, "send", lambda obj, timeout=0: {"data": {"ok": True, "result": {"approved": False, "decision": "denied", "reason": "no"}}})
    run = mock.Mock()
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.gated_execute("git.push", "push", ["/usr/bin/git", "push"]) == 3
    run.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="bash PATH wrapper executes only on Linux nodes")
def test_git_wrapper_intercepts_push_and_not_status(tmp_path: Path):
    home = tmp_path / "wb"
    (home / "bin").mkdir(parents=True)
    log = tmp_path / "agentctl.log"
    ctl = home / "bin" / "agentctl"
    ctl.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {str(log)!r}\n', encoding="utf-8")
    ctl.chmod(0o755)
    wrapper = ROOT / "linux" / "enforced-bin" / "git"
    env = os.environ.copy()
    env["WORKBOT_HOME"] = str(home)
    env["WORKBOT_REAL_GIT"] = "/bin/echo"
    cp = subprocess.run([str(wrapper), "push", "origin", "main"], env=env, text=True, capture_output=True)
    assert cp.returncode == 0
    text = log.read_text(encoding="utf-8")
    assert "gated-exec" in text and "--action git.push" in text
    assert "/bin/echo push origin main" in text
    cp2 = subprocess.run([str(wrapper), "status"], env=env, text=True, capture_output=True)
    assert cp2.returncode == 0
    assert cp2.stdout.strip() == "status"


def test_worker_store_migrates_v08_task_schema(tmp_path: Path):
    import sqlite3, sys
    db = tmp_path / "worker.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE tasks (
      task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, title TEXT, instruction TEXT,
      conversation_id TEXT, state TEXT NOT NULL, session_id TEXT, workdir TEXT,
      result_json TEXT, created_at INTEGER NOT NULL DEFAULT (unixepoch()),
      updated_at INTEGER NOT NULL DEFAULT (unixepoch())
    );
    CREATE TABLE outbox (
      event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      created_at INTEGER NOT NULL DEFAULT (unixepoch()), delivered_at INTEGER
    );
    """)
    c.commit(); c.close()
    sys.path.insert(0, str(ROOT / "linux"))
    from workbot_worker.store import WorkerStore
    s = WorkerStore(db)
    cols = {r[1] for r in s.all("PRAGMA table_info(tasks)")}
    assert "recovery_count" in cols
    assert "last_recovered_at" in cols
    assert "agent_session_id" in cols
