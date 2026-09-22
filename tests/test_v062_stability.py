import pytest

from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager, _summary_from_text
from workbot.conversation.manager import ConversationManager
from workbot.conversation.models import IncomingMessage
from workbot.storage.sqlite import Store


def _seed(cm: ConversationManager):
    cm.add_incoming(IncomingMessage(
        platform="welink", conversation_id="welink:group:1", conversation_kind="group",
        external_conversation_id="1", external_message_id="1", sender_id="u",
        content="当前任务已经成功", sent_at_ms=1, display_name="g",
    ))


def test_summary_parser_prefers_wrapper_and_drops_meta_preamble():
    assert _summary_from_text(
        "I will summarize this conversation now.\n\n用户当前工作流已成功完成。"
    ) == "用户当前工作流已成功完成。"
    assert _summary_from_text(
        "noise\n<WORKBOT_SUMMARY>用户当前没有活动任务。</WORKBOT_SUMMARY>\nmore noise"
    ) == "用户当前没有活动任务。"


@pytest.mark.asyncio
async def test_full_refresh_does_not_feed_previous_summary_back(tmp_path):
    store = Store(tmp_path / "w.db")
    cm = ConversationManager(store)
    _seed(cm)
    cm.set_summary("welink:group:1", "OLD_STALE_WINERROR_FAILURE", 1)
    am = AgentManager({}, tmp_path, cm)
    seen = []

    async def fake_run(prompt, session_id=None, *, new_session_id=None):
        seen.append(prompt)
        return AgentResult("<WORKBOT_SUMMARY>当前任务已经成功。</WORKBOT_SUMMARY>")

    am.backend.run = fake_run
    out = await am.summarize_conversation("welink:group:1", full_refresh=True)
    assert out == "当前任务已经成功。"
    assert "OLD_STALE_WINERROR_FAILURE" not in seen[0]
    assert "<ignored during full refresh>" in seen[0]


def test_store_uses_short_lived_connections_and_supports_transaction(tmp_path):
    store = Store(tmp_path / "w.db")
    store.execute("INSERT INTO processed_messages(platform,external_message_id) VALUES (?,?)", ("x", "1"))
    assert store.query_one("SELECT count(*) AS n FROM processed_messages")["n"] == 1
    with store.transaction() as conn:
        conn.execute("INSERT INTO processed_messages(platform,external_message_id) VALUES (?,?)", ("x", "2"))
    assert store.query_one("SELECT count(*) AS n FROM processed_messages")["n"] == 2
