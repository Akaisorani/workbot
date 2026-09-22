import asyncio
import json
from pathlib import Path
from unittest import mock

import pytest

from workbot.conversation.models import IncomingMessage
from workbot.main import WorkBot
from workbot.policy.approvals import ApprovalManager
from workbot.policy.engine import PolicyEngine
from workbot.storage.sqlite import Store
from workbot.transport.protocol import Message


def test_action_policy_defaults():
    p = PolicyEngine({}, {})
    assert p.evaluate_action("git.status").decision == "allow"
    assert p.evaluate_action("build.run").decision == "allow"
    assert p.evaluate_action("git.push").decision == "approve"
    assert p.evaluate_action("deploy.production").decision == "approve"
    assert p.evaluate_action("credential.export").decision == "deny"
    assert p.evaluate_action("some.new.side.effect").decision == "approve"


def test_action_policy_custom_first_match():
    p = PolicyEngine({}, {
        "default": "deny",
        "rules": [
            {"match": ["deploy.staging"], "decision": "allow"},
            {"match": ["deploy.*"], "decision": "approve"},
        ],
    })
    assert p.evaluate_action("deploy.staging").decision == "allow"
    assert p.evaluate_action("deploy.production").decision == "approve"
    assert p.evaluate_action("unknown").decision == "deny"


@pytest.mark.asyncio
async def test_approval_manager_wait_and_decide(tmp_path: Path):
    store = Store(tmp_path / "state.db")
    mgr = ApprovalManager(store, {"approver_senders": ["owner"], "approval_timeout_seconds": 5})
    aid = mgr.create(
        conversation_id="welink:group:g1", node="linux-server1", task_id="task-1",
        action="git.push", summary="push branch", requested_by="linux-server1",
    )
    waiter = asyncio.create_task(mgr.wait(aid))
    await asyncio.sleep(0)
    with pytest.raises(PermissionError):
        mgr.decide(aid, approved=True, sender_id="other")
    mgr.decide(aid, approved=True, sender_id="owner")
    result = await waiter
    assert result.approved is True
    row = mgr.get(aid)
    assert row["state"] == "approved"
    assert row["decided_by"] == "owner"


class FakeIM:
    def __init__(self):
        self.sent = []

    async def send_text(self, cid, text):
        self.sent.append((cid, text))


def make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {"cli": "welink-cli", "groups": [], "bootstrap_from_latest": True},
        "agent": {"command": "codeagent"},
        "nodes": {"linux-server1": {"ssh_alias": "linux-server1", "relay_command": "noop", "default_conversation_id": "welink:group:g1"}},
        "access_control": {"default": "deny", "allow_senders": ["owner"]},
        "action_policy": {"approver_senders": ["owner"], "approval_timeout_seconds": 5},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    bot = WorkBot(cp)
    bot.im = FakeIM()
    bot.ssh.send = mock.AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_remote_approval_request_blocks_until_im_approval(tmp_path: Path):
    bot = make_bot(tmp_path)
    req = Message(
        type="request", method="policy.request_approval", task_id="task-abcd12",
        source="linux-server1", target="workbot",
        data={"action": "git.push", "summary": "Push validated fix", "conversation_id": "welink:group:g1"},
    )
    await bot.on_remote_message("linux-server1", req)
    # Wait until the background request handler creates/sends the approval.
    for _ in range(50):
        rows = bot.approvals.pending(conversation_id="welink:group:g1")
        if rows and bot.im.sent:
            break
        await asyncio.sleep(0.01)
    assert rows
    aid = rows[0]["approval_id"]
    assert aid in bot.im.sent[-1][1]
    assert "git.push" in bot.im.sent[-1][1]
    assert bot.ssh.send.await_count == 0

    user = IncomingMessage(
        platform="welink", conversation_id="welink:group:g1", conversation_kind="group",
        external_conversation_id="g1", external_message_id="m2", sender_id="owner",
        content=f"/approve {aid}", sent_at_ms=2, display_name="g1",
    )
    handled = await bot._handle_lifecycle_command(user, user.content)
    assert handled is True
    for _ in range(50):
        if bot.ssh.send.await_count:
            break
        await asyncio.sleep(0.01)
    assert bot.ssh.send.await_count == 1
    reply = bot.ssh.send.await_args.args[1]
    assert reply.correlation_id == req.id
    assert reply.data["ok"] is True
    assert reply.data["result"]["approved"] is True
    assert reply.data["result"]["approval_id"] == aid


@pytest.mark.asyncio
async def test_remote_policy_auto_allow_and_deny_do_not_prompt(tmp_path: Path):
    bot = make_bot(tmp_path)
    allow_req = Message(type="request", method="policy.request_approval", source="linux-server1", target="workbot",
                        data={"action": "git.status", "summary": "status", "conversation_id": "welink:group:g1"})
    await bot.on_remote_message("linux-server1", allow_req)
    for _ in range(30):
        if bot.ssh.send.await_count >= 1:
            break
        await asyncio.sleep(0.01)
    allow_reply = bot.ssh.send.await_args_list[0].args[1]
    assert allow_reply.data["result"]["approved"] is True
    assert allow_reply.data["result"]["auto"] is True
    assert not bot.im.sent

    deny_req = Message(type="request", method="policy.request_approval", source="linux-server1", target="workbot",
                       data={"action": "credential.export", "summary": "export secret", "conversation_id": "welink:group:g1"})
    await bot.on_remote_message("linux-server1", deny_req)
    for _ in range(30):
        if bot.ssh.send.await_count >= 2:
            break
        await asyncio.sleep(0.01)
    deny_reply = bot.ssh.send.await_args_list[1].args[1]
    assert deny_reply.data["result"]["approved"] is False
    assert deny_reply.data["result"]["decision"] == "denied"
    assert not bot.im.sent

@pytest.mark.asyncio
async def test_windows_agent_approval_marker_is_parsed(tmp_path: Path):
    from workbot.agents.codeagent import AgentResult
    from workbot.agents.manager import AgentManager
    from workbot.conversation.manager import ConversationManager

    store = Store(tmp_path / "agent.db")
    conversations = ConversationManager(store)
    seed = IncomingMessage("welink", "welink:group:g1", "group", "g1", "m1", "owner", "push it", 1, "g1")
    conversations.add_incoming(seed)
    mgr = AgentManager({"command": "codeagent"}, tmp_path, conversations)

    async def fake_run(*args, **kwargs):
        sid = kwargs.get("session_id") or kwargs.get("new_session_id")
        return AgentResult(
            text='Need approval. <WORKBOT_APPROVAL>{"action":"git.push","summary":"push main","details":{"branch":"main"}}</WORKBOT_APPROVAL>',
            session_id=sid,
        )

    mgr.backend.run = fake_run
    result = await mgr.answer("welink:group:g1", "push it")
    assert result.approval["action"] == "git.push"
    assert "WORKBOT_APPROVAL" not in result.text


@pytest.mark.asyncio
async def test_windows_conversation_approval_resumes_same_session(tmp_path: Path):
    from workbot.agents.codeagent import AgentResult

    bot = make_bot(tmp_path)
    seed = IncomingMessage("welink", "welink:group:g1", "group", "g1", "m1", "owner", "push it", 1, "g1")
    bot.conversations.add_incoming(seed)
    calls = []

    async def fake_run(prompt, session_id=None, new_session_id=None):
        calls.append((session_id, new_session_id, prompt))
        sid = session_id or new_session_id
        if len(calls) == 1:
            return AgentResult(
                text='<WORKBOT_APPROVAL>{"action":"git.push","summary":"push main","details":{}}</WORKBOT_APPROVAL>',
                session_id=sid,
            )
        return AgentResult(text="push completed", session_id=sid)

    bot.agents.backend.run = fake_run
    result = await bot.agents.answer("welink:group:g1", "push it")
    assert result.approval
    await bot._handle_conversation_agent_approval("welink:group:g1", result.approval)
    rows = bot.approvals.pending(conversation_id="welink:group:g1")
    assert len(rows) == 1
    aid = rows[0]["approval_id"]
    approve_msg = IncomingMessage("welink", "welink:group:g1", "group", "g1", "m2", "owner", f"/approve {aid}", 2, "g1")
    await bot._handle_lifecycle_command(approve_msg, approve_msg.content)
    for _ in range(50):
        if any("push completed" in text for _, text in bot.im.sent):
            break
        await asyncio.sleep(0.01)
    assert any("push completed" in text for _, text in bot.im.sent)
    assert calls[0][1] is not None
    assert calls[1][0] == calls[0][1]
