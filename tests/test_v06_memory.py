from workbot.storage.sqlite import Store
from workbot.memory.manager import MemoryManager


def test_memory_dedup_search_scope_and_forget(tmp_path):
    store = Store(tmp_path / "w.db")
    mm = MemoryManager(store)
    a = mm.remember("global", "linux-server1用途", "linux-server1 是 Linux 开发服务器", tags="node,linux")
    b = mm.remember("global", "linux-server1用途", "linux-server1 是 Linux 开发服务器", tags="node,linux")
    assert a == b
    c = mm.remember("conversation:c1", "当前约定", "这个会话只讨论 parser", tags="parser")
    rows = mm.search("linux-server1", scopes=["global", "conversation:c1"])
    assert any(r["id"] == a for r in rows)
    scoped = mm.search("parser", scopes=["conversation:c1"])
    assert [r["id"] for r in scoped] == [c]
    assert mm.forget(c)
    assert mm.get(c) is None


def test_old_memory_rows_can_coexist_with_new_fingerprints(tmp_path):
    store = Store(tmp_path / "w.db")
    store.execute("INSERT INTO memory_items(scope,title,content,tags,source) VALUES ('global','old','legacy','','x')")
    mm = MemoryManager(store)
    new_id = mm.remember("global", "new", "durable decision")
    assert new_id > 0
    assert mm.search("legacy", limit=2)[0]["title"] == "old"


def test_auto_memory_metadata_and_summary_purge(tmp_path):
    store = Store(tmp_path / "w.db")
    mm = MemoryManager(store)
    a = mm.remember("conversation:c1", "auto", "durable", source="summary:c1",
                    evidence="explicit evidence", auto_generated=True, confidence=0.9)
    b = mm.remember("global", "manual", "keep", source="manual")
    row = mm.get(a)
    assert row["auto_generated"] == 1
    assert row["evidence"] == "explicit evidence"
    assert mm.forget_auto_summary() == 1
    assert mm.get(a) is None
    assert mm.get(b) is not None

import pytest
from workbot.agents.manager import AgentManager
from workbot.agents.codeagent import AgentResult
from workbot.conversation.manager import ConversationManager


@pytest.mark.asyncio
async def test_memory_curator_rejects_missing_evidence_and_global_by_default(tmp_path):
    store = Store(tmp_path / "w2.db")
    cm = ConversationManager(store)
    am = AgentManager({}, tmp_path, cm, MemoryManager(store))
    async def fake_run(prompt, session_id=None, *, new_session_id=None):
        return AgentResult('''<WORKBOT_MEMORIES>[
          {"scope":"global","kind":"fact","title":"grounded","content":"linux-server1 is configured","tags":"node","confidence":0.95,"evidence":"linux-server1 is configured"},
          {"scope":"global","kind":"lesson","title":"bad lesson","content":"always retry","tags":"","confidence":0.99,"evidence":"timeout happened once"},
          {"scope":"conversation","kind":"fact","title":"hallucinated","content":"x","tags":"","confidence":0.99,"evidence":"not in material"}
        ]</WORKBOT_MEMORIES>''')
    am.backend.run = fake_run
    rows = await am.extract_memories("c1", "status: linux-server1 is configured; timeout happened once", allow_global=False)
    assert len(rows) == 1
    assert rows[0]["title"] == "grounded"
    assert rows[0]["scope"] == "conversation:c1"
