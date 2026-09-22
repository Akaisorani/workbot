from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from workbot.agents.claims import detect_managed_state_claim
from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot
from workbot.memory.manager import MemoryManager
from workbot.storage.sqlite import Store
from workbot.tasks.manager import TaskManager
from workbot.transport.protocol import response


class SequenceBackend:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def run(self, prompt, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        sid = kwargs.get("session_id") or kwargs.get("new_session_id")
        return AgentResult(self.outputs.pop(0), session_id=sid)


def make_manager(tmp_path: Path, outputs):
    store = Store(tmp_path / "state.sqlite")
    conversations = ConversationManager(store)
    conversations.add_incoming(IncomingMessage(
        "welink", "welink:group:g", "group", "g", "m1", "owner",
        "处理上面的任务", 1, "g",
    ))
    manager = AgentManager({"command": "codeagent", "max_concurrent": 1}, tmp_path, conversations, MemoryManager(store, {}))
    manager.set_managed_node_names(["linux-server1", "linux-server1"])
    backend = SequenceBackend(outputs)
    manager.backend = backend
    return manager, backend, store


def test_managed_claim_detector_targets_remote_state_but_not_negative_explanation():
    claim = detect_managed_state_claim("已发起远程任务到 linux-server1 执行。", ["linux-server1"])
    assert claim is not None and claim.kind == "remote_task"
    assert detect_managed_state_claim("尚未发起远程任务，需要先创建 task。", ["linux-server1"]) is None
    assert detect_managed_state_claim("这句“已发起远程任务”是模型幻觉，并不代表真的创建。", ["linux-server1"]) is None
    # Generic local execution is intentionally outside this guard.
    assert detect_managed_state_claim("本地编译已经完成。", ["linux-server1"]) is None


@pytest.mark.asyncio
async def test_false_remote_claim_is_reexamined_into_structured_action(tmp_path: Path):
    manager, backend, _ = make_manager(tmp_path, [
        "已识别请求。已发起远程任务到 linux-server1 执行。",
        '<WORKBOT_ACTION>{"type":"remote_task","node":"linux-server1","instruction":"在 linux-server1 拉取 example_repo 最新代码然后编译"}</WORKBOT_ACTION>',
    ])

    result = await manager.answer("welink:group:g", "处理上面 example-user-a 的任务")

    assert result.action == {
        "type": "remote_task", "node": "linux-server1", "instruction": "在 linux-server1 拉取 example_repo 最新代码然后编译"
    }
    assert "已发起" not in result.text
    assert len(backend.calls) == 2
    assert backend.calls[1][1].get("tool_mode") == "disabled"
    assert "NO matching durable receipt" in backend.calls[1][0]
    assert "return the appropriate structured proposal" in backend.calls[1][0]


@pytest.mark.asyncio
async def test_false_claim_recovery_can_admit_not_started(tmp_path: Path):
    manager, backend, _ = make_manager(tmp_path, [
        "已发起远程任务到 linux-server1 执行。",
        "尚未发起远程任务。这个请求需要先由 WorkBot 创建任务并获得真实 task ID。",
    ])
    result = await manager.answer("welink:group:g", "做这个任务")
    assert result.action is None
    assert "尚未发起" in result.text
    assert len(backend.calls) == 2


@pytest.mark.asyncio
async def test_repeated_false_claim_is_replaced_by_safe_workbot_message(tmp_path: Path):
    manager, _, _ = make_manager(tmp_path, [
        "已发起远程任务到 linux-server1 执行。",
        "已发起远程任务到 linux-server1 执行。",
    ])
    result = await manager.answer("welink:group:g", "做这个任务")
    assert "没有找到真实的 task/workflow receipt" in result.text
    assert "尚未由 WorkBot 确认启动" in result.text


@pytest.mark.asyncio
async def test_existing_task_receipt_allows_grounded_status_without_recovery(tmp_path: Path):
    manager, backend, store = make_manager(tmp_path, ["远端任务 task-deadbeef 正在 linux-server1 执行。"])
    store.execute(
        "INSERT INTO tasks(task_id,node,task_type,state,title,instruction) VALUES (?,?,?,?,?,?)",
        ("task-deadbeef", "linux-server1", "codeagent", "running", "t", "x"),
    )
    result = await manager.answer("welink:group:g", "task-deadbeef 状态")
    assert result.text.startswith("远端任务 task-deadbeef")
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_action_preamble_cannot_claim_task_already_started(tmp_path: Path):
    manager, backend, _ = make_manager(tmp_path, [
        '已发起远程任务到 linux-server1 执行。\n<WORKBOT_ACTION>{"type":"remote_task","node":"linux-server1","instruction":"make"}</WORKBOT_ACTION>'
    ])
    result = await manager.answer("welink:group:g", "在linux-server1执行make")
    assert result.action and result.action["type"] == "remote_task"
    assert "已发起" not in result.text
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_task_manager_persists_and_forwards_request_authorization_provenance(tmp_path: Path):
    store = Store(tmp_path / "task.sqlite")
    tasks = TaskManager(store)

    async def fake_request(node, req, timeout=20):
        assert node == "linux-server1"
        assert req.data["requested_by"] == "example-user-a"
        assert req.data["authorized_by"] == "example-user-b"
        return response(req, data={"accepted": True, "agent_session_id": "agent-s1"})

    tasks._request = fake_request
    task_id = await tasks.create_remote_task(
        node="linux-server1", conversation_id="welink:group:g", origin_message_id="100",
        title="远端任务", instruction="compile", requested_by="example-user-a",
        authorized_by="example-user-b", authorization_message_id="200",
    )
    row = store.query_one(
        "SELECT requested_by,authorized_by,authorization_message_id,state FROM tasks WHERE task_id=?",
        (task_id,),
    )
    assert dict(row) == {
        "requested_by": "example-user-a",
        "authorized_by": "example-user-b",
        "authorization_message_id": "200",
        "state": "running",
    }


class FakeIM:
    def __init__(self):
        self.sent = []

    async def send_text(self, conversation_id, text):
        self.sent.append((conversation_id, text))


def make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {"type": "welink", "groups": [], "intent": {"require_alias": False, "bot_aliases": ["bot", "WorkBot"]}},
        "execution_policy": {"default": "deny", "allow_senders": ["example-user-b"]},
        "reply_policy": {"default": "allow", "silent_if_unfulfillable": True},
        "agent": {"command": "codeagent", "max_concurrent": 1},
        "default_node": "linux-server1",
        "nodes": {"linux-server1": {"ssh_alias": "linux-server1", "relay_command": "noop"}},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = FakeIM()
    return bot


def imsg(mid: str, sender: str, content: str) -> IncomingMessage:
    return IncomingMessage(
        platform="welink", conversation_id="welink:group:1105", conversation_kind="group",
        external_conversation_id="1105", external_message_id=mid,
        sender_id=sender, content=content, sent_at_ms=int(mid), display_name="group",
    )


@pytest.mark.asyncio
async def test_operator_can_take_over_recent_request_and_provenance_is_preserved(tmp_path: Path):
    bot = make_bot(tmp_path)
    original = imsg("100", "example-user-a", "bot 在 linux-server1 拉一下 example_repo 最新代码然后编译")
    assert bot.conversations.add_incoming(original) is True

    operator = imsg("200", "example-user-b", "处理上面 example-user-a 的任务")
    await bot.handle_im_message(operator)

    pending = bot.conversations.get_pending_action(operator.conversation_id)
    assert pending is not None and pending.action_type == "task.create"
    assert pending.payload["node"] == "linux-server1"
    assert pending.payload["instruction"] == "在 linux-server1 拉一下 example_repo 最新代码然后编译"
    assert pending.payload["requested_by"] == "example-user-a"
    assert pending.payload["authorized_by"] == "example-user-b"
    assert pending.payload["origin_message_id"] == "100"
    assert pending.payload["authorization_message_id"] == "200"
    assert "原始请求者：example-user-a" in bot.im.sent[-1][1]
    assert "执行授权者：example-user-b" in bot.im.sent[-1][1]

    async def fake_create_remote_task(**kwargs):
        assert kwargs["requested_by"] == "example-user-a"
        assert kwargs["authorized_by"] == "example-user-b"
        assert kwargs["origin_message_id"] == "100"
        assert kwargs["authorization_message_id"] == "200"
        return "task-abc123"

    with mock.patch.object(bot.tasks, "create_remote_task", side_effect=fake_create_remote_task):
        await bot.handle_im_message(imsg("201", "example-user-b", "创建"))
    assert bot.im.sent[-1][1] == "已创建远端任务 task-abc123，正在 linux-server1 执行。"


@pytest.mark.asyncio
async def test_delegated_request_does_not_use_target_users_old_permission(tmp_path: Path):
    bot = make_bot(tmp_path)
    original = imsg("100", "example-user-a", "在 linux-server1 编译 example_repo")
    assert bot.conversations.add_incoming(original)
    # Another unprivileged sender cannot authorize the stored request.
    await bot.handle_im_message(imsg("200", "example-user-c", "处理上面 example-user-a 的任务"))
    assert bot.conversations.get_pending_action(original.conversation_id) is None
