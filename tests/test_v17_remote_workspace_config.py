import json
from pathlib import Path

import pytest

from linux.workbot_worker.workspace import discover_projects, scan_project
from workbot.configuration import compact_config, dumps_compact
from workbot.knowledge.workspace import WorkspaceKnowledgeManager
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def analyze_workspace_project(self, root, material):
        self.calls.append((root, material))
        return {
            "summary": "Remote demo service with bounded workspace indexing.",
            "structure": "README and src/app.py are indexed; deeper generated/source paths are bounded.",
            "keywords": ["remote-demo", "src/app.py"],
        }


def make_remote_tree(tmp_path: Path):
    server = tmp_path / "server"
    p1 = server / "projects" / "demo"
    p2 = server / "projects" / "other"
    for p in (p1, p2):
        p.mkdir(parents=True)
        (p / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (p1 / "README.md").write_text("# Remote demo\nhandles remote widgets", encoding="utf-8")
    (p1 / "src").mkdir()
    (p1 / "src" / "app.py").write_text("def remote_widget():\n    return 'remote'\n", encoding="utf-8")
    (p1 / "src" / "deep").mkdir()
    (p1 / "src" / "deep" / "hidden.py").write_text("SHOULD_NOT_INDEX = True\n", encoding="utf-8")
    (p1 / ".env").write_text("SECRET=do-not-index\n", encoding="utf-8")
    return server, p1, p2


def test_worker_remote_workspace_discovery_and_depth_limits(tmp_path: Path):
    server, p1, _p2 = make_remote_tree(tmp_path)
    result = discover_projects([str(server / "projects")], {
        "project_discovery_depth": 2,
        "max_projects": 1,
    })
    assert len(result["projects"]) == 1
    assert result["truncated"] is True

    snap = scan_project([str(server / "projects")], str(p1), {
        "index_path_depth": 1,
        "max_files_per_project": 100,
        "max_chunks_per_project": 20,
        "max_index_chars_per_project": 100000,
    })
    rels = {x["relative_path"] for x in snap["files"]}
    assert "README.md" in rels
    assert "src/app.py" in rels
    assert "src/deep/hidden.py" not in rels
    assert ".env" not in rels
    assert snap["returned_chunks"] > 0

    known = {
        x["relative_path"]: {
            "size": x["size"], "mtime_ns": x["mtime_ns"], "content_hash": x.get("content_hash", "")
        }
        for x in snap["files"]
    }
    snap2 = scan_project([str(server / "projects")], str(p1), {
        "index_path_depth": 1,
        "max_files_per_project": 100,
        "max_chunks_per_project": 20,
    }, known)
    assert all(x["changed"] is False for x in snap2["files"])
    assert snap2["returned_chunks"] == 0


@pytest.mark.asyncio
async def test_workspace_manager_indexes_remote_node_into_unified_search(tmp_path: Path):
    server, p1, _p2 = make_remote_tree(tmp_path)
    store = Store(tmp_path / "db.sqlite")
    memory = MemoryManager(store, {})

    async def node_request(node, method, data, timeout=30):
        assert node == "linux-server1"
        if method == "workspace.discover":
            return discover_projects([str(server / "projects")], data)
        if method == "workspace.scan":
            return scan_project(
                [str(server / "projects")], data["project_root"], data.get("options"), data.get("known")
            )
        raise AssertionError(method)

    wk = WorkspaceKnowledgeManager(
        store, memory,
        {"enabled": True, "remote_defaults": {"index_path_depth": 1, "max_projects": 10}},
        [],
        node_configs={
            "linux-server1": {
                "ssh_alias": "linux-server1",
                "workspace_knowledge_roots": [str(server / "projects")],
                "workspace_knowledge": {"index_path_depth": 1, "max_projects": 10},
            }
        },
        node_request=node_request,
        node_online=lambda node: True,
    )
    agent = FakeAgent()
    assert await wk.maintenance_once(agent) is True
    assert agent.calls
    project_root = agent.calls[0][0]
    assert project_root.startswith("ssh://linux-server1/")

    rows = store.query_all("SELECT path,relative_path FROM workspace_files WHERE project_root=?", (project_root,))
    rels = {r["relative_path"] for r in rows}
    assert "src/app.py" in rels
    assert "src/deep/hidden.py" not in rels
    assert all(str(r["path"]).startswith("ssh://linux-server1/") for r in rows)

    hits = wk.search("remote_widget", limit=5)
    assert any(h["path"].startswith("ssh://linux-server1/") and h["path"].endswith("src/app.py") for h in hits)
    status = wk.status()
    assert status["sources"]["linux-server1"]["projects"] >= 1
    assert status["sources"]["linux-server1"]["online"] is True
    mem = store.query_one("SELECT content FROM memory_items WHERE source LIKE 'workspace-project:ssh://linux-server1/%'")
    assert mem is not None and "ssh://linux-server1/" in mem["content"]


def test_config_compaction_is_conservative_and_tighter():
    cfg = {
        "database": "state/workbot.db",
        "poll_interval_seconds": 3,
        "im": {"intent": {"enabled": True, "require_alias": True, "bot_aliases": ["bot", "WorkBot"]}},
        "workspace_knowledge": {
            "enabled": True,
            "roots": [],
            "idle_grace_seconds": 300,
            "scan_interval_seconds": 21600,
        },
        "nodes": {
            "linux-server1": {
                "ssh_alias": "linux-server1",
                "relay_command": "python3 -u ~/.workbot/worker_main.py relay",
                "max_concurrent": 4,
                "priority": 100,
                "enabled": True,
                "labels": [],
                "routing_hints": [],
                "workspace_knowledge_roots": ["/home/workbot/code", "/data/repo"],
                "workspace_knowledge": {"index_path_depth": 5, "max_projects": 99},
                "custom_unknown_key": {"keep": True},
            }
        },
        "reply_policy": {"default": "allow", "deny_senders": ["x"]},
    }
    compacted, removed = compact_config(cfg)
    assert removed >= 8
    assert "database" not in compacted
    assert compacted["workspace_knowledge"] == {"enabled": True}
    node = compacted["nodes"]["linux-server1"]
    assert "relay_command" not in node and "max_concurrent" not in node and "enabled" not in node
    assert node["workspace_knowledge_roots"] == ["/home/workbot/code", "/data/repo"]
    assert node["workspace_knowledge"] == {"max_projects": 99}
    assert node["custom_unknown_key"] == {"keep": True}
    assert compacted["reply_policy"]["deny_senders"] == ["x"]
    rendered = dumps_compact(compacted)
    assert '"bot_aliases": ["bot", "WorkBot"]' in rendered
    assert '"workspace_knowledge_roots": ["/home/workbot/code", "/data/repo"]' in rendered
    json.loads(rendered)


@pytest.mark.asyncio
async def test_workspace_search_prefers_earlier_specific_root_over_download_archive(tmp_path: Path):
    current = tmp_path / "code" / "workbot"
    archive_root = tmp_path / "Downloads"
    archive = archive_root / "workbot_v1_0" / "workbot"
    for p in (current, archive):
        p.mkdir(parents=True)
        (p / "pyproject.toml").write_text("[project]\nname='workbot'\n", encoding="utf-8")
        (p / "README.md").write_text("# WorkBot\n", encoding="utf-8")
    store = Store(tmp_path / "rank.db")
    memory = MemoryManager(store, {})
    wk = WorkspaceKnowledgeManager(store, memory, {"enabled": True}, [current, archive_root])
    agent = FakeAgent()
    # Index both projects (maintenance processes one per call).
    assert await wk.maintenance_once(agent)
    assert await wk.maintenance_once(agent)
    hits = wk.search("WorkBot", limit=5)
    project_hits = [h for h in hits if h["type"] == "project"]
    assert project_hits
    assert Path(project_hits[0]["path"]).resolve() == current.resolve()


def test_remote_virtual_path_roundtrips_windows_drive_paths():
    virtual = WorkspaceKnowledgeManager._remote_virtual_root("linux-server1", r"C:\Users\tester\repo\demo")
    assert virtual == "ssh://linux-server1/C:/Users/tester/repo/demo"
    assert WorkspaceKnowledgeManager._parse_remote_root(virtual) == (
        "linux-server1", "C:/Users/tester/repo/demo"
    )
    linux_virtual = WorkspaceKnowledgeManager._remote_virtual_root("linux-server1", "/home/workbot/code/demo")
    assert linux_virtual == "ssh://linux-server1/home/workbot/code/demo"
    assert WorkspaceKnowledgeManager._parse_remote_root(linux_virtual) == (
        "linux-server1", "/home/workbot/code/demo"
    )


def test_configure_node_workspace_powershell_keeps_node_name_and_verifies_persistence():
    script = (Path(__file__).parents[1] / "scripts" / "configure-node-workspace.ps1").read_text(encoding="utf-8")
    # PowerShell variables are case-insensitive: `$node = ...` aliases the
    # `$Node` parameter.  V1.7.1 must keep the string name in a distinct var.
    assert "$NodeName = [string]$Node" in script
    assert "$nodeCfg = $prop.Value" in script
    assert "$node = $prop.Value" not in script
    assert "Remote workspace roots were not persisted" in script
    assert "install-node.ps1 -Node $NodeName" in script


def test_compaction_keeps_remote_roots_when_all_bounds_are_defaults():
    cfg = {
        "nodes": {
            "linux-server1": {
                "ssh_alias": "linux-server1",
                "workspace_knowledge_roots": ["/data/workbot/code", "/home/workbot"],
                "workspace_knowledge": {
                    "project_discovery_depth": 2,
                    "index_path_depth": 5,
                    "max_projects": 20,
                    "max_files_per_project": 1200,
                    "max_chunks_per_project": 160,
                    "max_index_chars_per_project": 600000,
                },
            }
        }
    }
    compacted, _removed = compact_config(cfg)
    node = compacted["nodes"]["linux-server1"]
    assert node["workspace_knowledge_roots"] == ["/data/workbot/code", "/home/workbot"]
    assert "workspace_knowledge" not in node
