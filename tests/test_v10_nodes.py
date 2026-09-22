from pathlib import Path

from workbot.nodes.registry import NodeRegistry
from workbot.storage.sqlite import Store


def make_registry(tmp_path: Path):
    store = Store(tmp_path / "workbot.db")
    cfg = {
        "arm": {
            "description": "ARM64 dev node",
            "capabilities": ["linux", "aarch64", "codeagent"],
            "labels": ["arm64"],
            "routing_hints": ["arm", "aarch64"],
            "priority": 100,
            "max_concurrent": 2,
        },
        "gpu": {
            "description": "GPU dev node",
            "capabilities": ["linux", "cuda", "gpu", "codeagent"],
            "labels": ["a800"],
            "routing_hints": ["cuda", "gpu", "a800"],
            "priority": 100,
            "max_concurrent": 4,
        },
    }
    reg = NodeRegistry(cfg, store, default_node="arm")
    return store, reg


def test_node_registry_prefers_capability_hint_and_online(tmp_path: Path):
    _store, reg = make_registry(tmp_path)
    reg.set_connected("arm", True)
    reg.set_connected("gpu", True)
    assert reg.choose("run CUDA benchmark on GPU") == "gpu"
    assert reg.choose("compile aarch64 code") == "arm"


def test_node_registry_uses_load_as_tiebreaker(tmp_path: Path):
    store, reg = make_registry(tmp_path)
    reg.set_connected("arm", True)
    reg.set_connected("gpu", True)
    for i in range(2):
        store.execute(
            "INSERT INTO tasks(task_id,node,task_type,state,title,instruction) VALUES (?,?, 'codeagent','running','x','x')",
            (f"task-load{i}", "arm"),
        )
    assert reg.choose("generic linux work", preferred=None) == "gpu"


def test_node_registry_does_not_route_to_offline_only_node(tmp_path: Path):
    _store, reg = make_registry(tmp_path)
    reg.set_connected("arm", False)
    reg.set_connected("gpu", False)
    assert reg.choose("generic work") is None


def test_planner_configs_include_runtime_state(tmp_path: Path):
    store, reg = make_registry(tmp_path)
    reg.set_connected("gpu", True)
    store.execute(
        "INSERT INTO tasks(task_id,node,task_type,state,title,instruction) VALUES ('task-x','gpu','codeagent','running','x','x')"
    )
    cfg = reg.planner_configs()["gpu"]
    assert cfg["runtime_online"] is True
    assert cfg["runtime_active_tasks"] == 1
    assert cfg["runtime_max_concurrent"] == 4
