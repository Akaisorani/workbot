import time
import pytest

from workbot.agents.manager import AgentManager
from workbot.agents.codeagent import AgentResult
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.storage.sqlite import Store


def seed_conversation(cm):
    msg = IncomingMessage(
        platform="welink", conversation_id="welink:group:1", conversation_kind="group",
        external_conversation_id="1", external_message_id="1", sender_id="u",
        content="hello", sent_at_ms=1, display_name="g",
    )
    cm.add_incoming(msg)


@pytest.mark.asyncio
async def test_session_rotation_uses_new_session_after_turn_limit(tmp_path):
    store = Store(tmp_path / "w.db")
    cm = ConversationManager(store)
    seed_conversation(cm)
    cm.set_agent_session("welink:group:1", "a256b50d-e3df-4415-829f-e1db0e4a906f")
    store.execute("UPDATE conversations SET session_turns=3 WHERE conversation_id=?", ("welink:group:1",))
    am = AgentManager({"max_session_turns": 3}, tmp_path, cm)
    seen = []
    async def fake_run(prompt, session_id=None, *, new_session_id=None):
        seen.append((session_id, new_session_id))
        return AgentResult("ok", session_id=new_session_id or session_id)
    am.backend.run = fake_run
    await am._run_with_conversation_session("welink:group:1", "x")
    assert len(seen) == 1
    assert seen[0][0] is None
    assert seen[0][1] is not None
    row = cm.get("welink:group:1")
    assert row["agent_session_id"] == seen[0][1]
    assert row["session_turns"] == 1


def test_conversation_summary_fields(tmp_path):
    store = Store(tmp_path / "w.db")
    cm = ConversationManager(store)
    seed_conversation(cm)
    cm.add_outgoing("welink:group:1", "reply", "out-1")
    assert cm.message_count("welink:group:1") == 2
    cm.set_summary("welink:group:1", "正在讨论工作流", 2)
    row = cm.get("welink:group:1")
    assert row["summary"] == "正在讨论工作流"
    assert row["summary_message_count"] == 2
