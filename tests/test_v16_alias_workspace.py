import json
from pathlib import Path
from unittest import mock

import pytest

from workbot.conversation.models import IncomingMessage
from workbot.knowledge.workspace import WorkspaceKnowledgeManager
from workbot.main import WorkBot
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store


def make_bot(tmp_path: Path):
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {
            "cli": "welink-cli", "groups": [], "bootstrap_from_latest": True,
            "intent": {"enabled": True, "require_alias": True, "bot_aliases": ["bot", "WorkBot"]},
        },
        "ingestion_policy": {"default": "allow"},
        "reply_policy": {"default": "allow", "silent_if_unfulfillable": True},
        "execution_policy": {"default": "deny", "allow_senders": ["owner"]},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "rpc": {"enabled": False, "windows_read_roots": [str(tmp_path)]},
        "workspace_knowledge": {"enabled": False},
        "nodes": {},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = mock.AsyncMock()
    return bot


def msg(mid: str, text: str, *, kind="group", sender="peer", at=False):
    cid = "welink:group:g" if kind == "group" else "welink:user:peer"
    ext = "g" if kind == "group" else "peer"
    return IncomingMessage("welink", cid, kind, ext, mid, sender, text, int(mid), "Test", is_at=at, transport="welinkbot")


@pytest.mark.asyncio
async def test_require_alias_still_ingests_every_message_but_dispatches_only_addressed(tmp_path: Path):
    bot = make_bot(tmp_path)
    queued = []
    bot._enqueue_conversation_message = lambda m: queued.append((m.external_message_id, m.content))

    plain = msg("1", "大家下午开会")
    addressed = msg("2", "bot 你好，帮我看一下")
    assert await bot._ingest_im_message(plain) is True
    bot._dispatch_im_message(plain)
    assert await bot._ingest_im_message(addressed) is True
    bot._dispatch_im_message(addressed)

    assert bot.conversations.message_count("welink:group:g") == 2
    assert queued == [("2", "你好，帮我看一下")]


@pytest.mark.asyncio
async def test_require_alias_applies_to_private_and_isat_does_not_bypass(tmp_path: Path):
    bot = make_bot(tmp_path)
    queued=[]
    bot._enqueue_conversation_message=lambda m: queued.append(m.content)
    bot._dispatch_im_message(msg("1", "你好", kind="user"))
    bot._dispatch_im_message(msg("2", "没有alias但isAt", at=True))
    bot._dispatch_im_message(msg("3", "WorkBot 你好", kind="user"))
    assert queued == ["你好"]


def test_alias_must_be_first_text_token_and_keeps_identifier_boundaries(tmp_path: Path):
    bot = make_bot(tmp_path)
    assert bot._strip_bot_alias("/tmp/workbot-test/output.log")[0] is False
    assert bot._strip_bot_alias("robot status")[0] is False
    assert bot._strip_bot_alias("C:\\example\\WorkBot\\state")[0] is False
    assert bot._strip_bot_alias("你好bot，查一下")[0] is False
    assert bot._strip_bot_alias("没有bot也会了吗")[0] is False
    assert bot._strip_bot_alias("   bot 你好") == (True, "你好")
    assert bot._strip_bot_alias("bot你好") == (True, "你好")
    assert bot._strip_bot_alias("bot: /approve approval-abcdef12") == (True, "/approve approval-abcdef12")


class FakeWorkspaceAgent:
    def __init__(self): self.calls=[]
    async def analyze_workspace_project(self, root, material):
        self.calls.append((root, material))
        return {
            "summary": "Demo service implemented in Python with a src package.",
            "structure": "README.md explains usage; src/app.py is the main implementation; tests contains tests.",
            "keywords": ["demo", "src/app.py", "pytest"],
        }


@pytest.mark.asyncio
async def test_workspace_knowledge_indexes_read_roots_and_updates_project_memory(tmp_path: Path):
    root=tmp_path / "code"
    proj=root / "demo"
    (proj / "src").mkdir(parents=True)
    (proj / "tests").mkdir()
    (proj / "README.md").write_text("# Demo\nThis service handles widgets.", encoding="utf-8")
    (proj / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (proj / "src" / "app.py").write_text("def widget_router():\n    return 'widget route'\n", encoding="utf-8")
    (proj / "tests" / "test_app.py").write_text("def test_widget():\n    assert True\n", encoding="utf-8")
    (proj / ".env").write_text("SECRET=must-not-index", encoding="utf-8")

    store=Store(tmp_path / "state.db")
    memory=MemoryManager(store,{})
    wk=WorkspaceKnowledgeManager(store,memory,{
        "enabled": True,
        "project_discovery_depth": 2,
        "scan_interval_seconds": 300,
        "max_file_bytes": 100000,
    },[root])
    agent=FakeWorkspaceAgent()
    assert await wk.maintenance_once(agent) is True
    assert len(agent.calls)==1

    paths={r["relative_path"] for r in store.query_all("SELECT relative_path FROM workspace_files")}
    assert "README.md" in paths
    assert "src/app.py" in paths
    assert ".env" not in paths
    hits=wk.search("widget_router",limit=5)
    assert any(str(h["path"]).endswith("app.py") for h in hits)
    mem=store.query_one("SELECT * FROM memory_items WHERE source LIKE 'workspace-project:%'")
    assert mem is not None
    assert "src/app.py" in mem["content"]
    assert "SECRET" not in "\n".join(str(r["content"]) for r in store.query_all("SELECT content FROM workspace_chunks"))


def test_workspace_schema_migrates_and_status(tmp_path: Path):
    store=Store(tmp_path / "db.sqlite")
    for table in ("workspace_projects","workspace_files","workspace_chunks"):
        row=store.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,))
        assert row is not None


def test_workspace_project_memory_is_updated_in_place(tmp_path: Path):
    store=Store(tmp_path / "db2.sqlite")
    memory=MemoryManager(store,{})
    a=memory.upsert_source("global","Workspace project: demo","v1",source="workspace-project:C:/demo",memory_kind="workspace",auto_generated=True)
    b=memory.upsert_source("global","Workspace project: demo","v2",source="workspace-project:C:/demo",memory_kind="workspace",auto_generated=True)
    assert a == b
    rows=store.query_all("SELECT * FROM memory_items WHERE source='workspace-project:C:/demo'")
    assert len(rows)==1 and rows[0]["content"]=="v2"

@pytest.mark.asyncio
async def test_strict_alias_does_not_change_ingestion_policy_and_known_slash_bypasses_alias(tmp_path: Path):
    bot = make_bot(tmp_path)
    touched=[]

    def fake_spawn(coro, **_kwargs):
        touched.append("spawn")
        try:
            coro.close()
        except Exception:
            pass
        return mock.Mock(done=lambda: True)

    bot._spawn = fake_spawn
    # Ordinary unaddressed DM is still ingested, but dispatch is store-only.
    plain = msg("10", "这是一条普通私聊", kind="user")
    assert await bot._ingest_im_message(plain) is True
    bot._dispatch_im_message(plain)
    assert bot.conversations.message_count("welink:user:peer") == 1
    assert touched == []

    # V1.10.2: known WorkBot slash controls intentionally bypass the alias gate;
    # unknown slash strings/media protocols are rejected by the command whitelist.
    slash = msg("11", "/status", kind="user", sender="owner")
    assert await bot._ingest_im_message(slash) is True
    bot._dispatch_im_message(slash)
    assert bot.conversations.message_count("welink:user:peer") == 2
    assert touched == ["spawn"]

    addressed_slash = msg("12", " bot /status", kind="user", sender="owner")
    assert await bot._ingest_im_message(addressed_slash) is True
    bot._dispatch_im_message(addressed_slash)
    assert touched == ["spawn", "spawn"]


def test_strict_alias_directed_logic_requires_alias_before_slash(tmp_path: Path):
    bot = make_bot(tmp_path)
    assert bot._directed_to_bot(msg("20", "/approve approval-abcdef12", sender="owner")) is False
    assert bot._directed_to_bot(msg("21", "你好", kind="user")) is False
    assert bot._directed_to_bot(msg("22", "bot 你好", kind="user")) is True
    assert bot._directed_to_bot(msg("23", " bot /approve approval-abcdef12", sender="owner")) is True

@pytest.mark.asyncio
async def test_workspace_search_handles_natural_language_terms(tmp_path: Path):
    root = tmp_path / "repos"
    proj = root / "WorkBot"
    (proj / "workbot" / "im").mkdir(parents=True)
    (proj / "README.md").write_text("# WorkBot\nWeLink realtime messaging and task orchestration.", encoding="utf-8")
    (proj / "pyproject.toml").write_text("[project]\nname='workbot'\n", encoding="utf-8")
    target = proj / "workbot" / "im" / "welink.py"
    target.write_text("def query_history_message():\n    # WeLink history polling fallback\n    pass\n", encoding="utf-8")

    store = Store(tmp_path / "knowledge.db")
    memory = MemoryManager(store, {})
    wk = WorkspaceKnowledgeManager(store, memory, {"enabled": True, "project_discovery_depth": 2}, [root])
    agent = FakeWorkspaceAgent()
    assert await wk.maintenance_once(agent)
    hits = wk.search("帮我找 WorkBot 里 WeLink 的轮询代码", limit=8)
    assert any(str(h["path"]).endswith("welink.py") for h in hits)
    assert any(h["type"] == "project" and str(h["path"]).endswith("WorkBot") for h in hits)


def test_workspace_knowledge_runs_only_after_idle_or_in_night_window(tmp_path: Path):
    bot = make_bot(tmp_path)
    bot.workspace_knowledge.enabled = True
    bot._knowledge_night_start = 0
    bot._knowledge_night_end = 0  # empty night window for deterministic test
    bot._knowledge_idle_grace = 300
    import time as _time
    bot._last_interactive_activity = _time.monotonic()
    assert bot._workspace_knowledge_window_open() is False
    bot._last_interactive_activity = _time.monotonic() - 301
    assert bot._workspace_knowledge_window_open() is True


def test_agent_context_includes_workspace_search_hits(tmp_path: Path):
    from workbot.agents.manager import AgentManager
    from workbot.conversation.manager import ConversationManager
    store=Store(tmp_path / "ctx.sqlite")
    conversations=ConversationManager(store)
    seed=IncomingMessage("welink","welink:user:u","user","u","1","u","bot 查 widget_router",1,"u")
    conversations.add_incoming(seed)
    memory=MemoryManager(store,{})
    agent=AgentManager({"command":"codeagent"},tmp_path,conversations,memory)
    class WK:
        def search(self,q,limit=6):
            return [{"type":"file","path":"C:/code/demo/src/app.py","excerpt":"def widget_router(): ..."}]
    agent.set_workspace_knowledge(WK())
    _env,_summary,memory_text,_recent,_active=agent._base_context("welink:user:u","widget_router 在哪里")
    assert "C:/code/demo/src/app.py" in memory_text
    assert "widget_router" in memory_text

class FlakyWorkspaceAgent:
    def __init__(self):
        self.calls = 0
    async def analyze_workspace_project(self, root, material):
        self.calls += 1
        if self.calls == 1:
            return {}
        return {"summary": "Recovered project summary", "structure": "src/main.py", "keywords": ["recovered"]}


@pytest.mark.asyncio
async def test_workspace_failed_analysis_is_retried_without_waiting_full_scan_interval(tmp_path: Path):
    root = tmp_path / "repos"
    proj = root / "retry-demo"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text("# retry demo", encoding="utf-8")
    (proj / "pyproject.toml").write_text("[project]\nname='retry-demo'\n", encoding="utf-8")
    (proj / "main.py").write_text("print('hello')\n", encoding="utf-8")
    store = Store(tmp_path / "retry.db")
    memory = MemoryManager(store, {})
    wk = WorkspaceKnowledgeManager(store, memory, {"enabled": True, "scan_interval_seconds": 21600}, [root])
    agent = FlakyWorkspaceAgent()
    assert await wk.maintenance_once(agent) is True
    assert agent.calls == 1
    assert wk.status()["pending_analysis"] == 1
    # analyzed_fingerprint mismatch makes the project immediately due again.
    assert await wk.maintenance_once(agent) is True
    assert agent.calls == 2
    assert wk.status()["pending_analysis"] == 0
    row = store.query_one("SELECT summary,analyzed_fingerprint,fingerprint FROM workspace_projects WHERE root_path=?", (str(proj),))
    assert row["summary"] == "Recovered project summary"
    assert row["analyzed_fingerprint"] == row["fingerprint"]


def test_broad_read_root_readme_does_not_hide_nested_project_roots(tmp_path: Path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "README.md").write_text("personal code folder", encoding="utf-8")
    proj = root / "sample-project"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='sample-project'", encoding="utf-8")
    (proj / "main.py").write_text("print('x')", encoding="utf-8")
    store = Store(tmp_path / "discover.db")
    memory = MemoryManager(store, {})
    wk = WorkspaceKnowledgeManager(store, memory, {"enabled": True, "project_discovery_depth": 2}, [root])
    projects = wk.discover_projects(force=True)
    assert proj.resolve() in projects
    assert root.resolve() not in projects


def test_workspace_relative_paths_are_normalized_on_store_migration(tmp_path: Path):
    db = tmp_path / "normalize.sqlite"
    store = Store(db)
    store.execute(
        "INSERT INTO workspace_projects(root_path,display_name) VALUES (?,?)",
        (r"C:\code\demo", "demo"),
    )
    store.execute(
        "INSERT INTO workspace_files(path,project_root,relative_path,size,mtime_ns,file_kind) VALUES (?,?,?,?,?,?)",
        (r"C:\code\demo\src\app.py", r"C:\code\demo", r"src\app.py", 1, 1, "py"),
    )
    # Re-opening the Store applies the V1.6.1 normalization migration.
    store = Store(db)
    row = store.query_one("SELECT relative_path FROM workspace_files WHERE path=?", (r"C:\code\demo\src\app.py",))
    assert row is not None
    assert row["relative_path"] == "src/app.py"
