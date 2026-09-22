from __future__ import annotations

from pathlib import Path

import pytest

from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager
from workbot.conversation.manager import ConversationManager
from workbot.memory.manager import MemoryManager
from workbot.rag.chunking import chunk_document
from workbot.rag.embedding import HashEmbeddingProvider, SentenceTransformerEmbeddingProvider
from workbot.rag.service import RAGHit, RAGService
from workbot.storage.sqlite import Store


class CaptureBackend:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls: list[tuple[str, dict]] = []

    async def run(self, prompt: str, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        text = self.outputs.pop(0)
        sid = kwargs.get("session_id") or kwargs.get("new_session_id")
        return AgentResult(text, session_id=sid)


class ActiveOnlyFakeRAG:
    enabled = True
    passive_enabled = False
    active_enabled = True
    max_active_rounds = 2
    cfg = {"active": {"max_top_k": 16}}

    def __init__(self):
        self.calls = []

    def search(self, query: str, **kwargs):
        self.calls.append((query, dict(kwargs)))
        return [RAGHit(
            chunk_id=1, document_id=1, source_type="code", source_key="workspace:x.cpp",
            namespace=r"C:\example\workspace\source", title="x.cpp", source_uri=r"C:\example\workspace\source\x.cpp",
            content="Document: x.cpp\nSymbol: ExecFoo\n\nvoid ExecFoo() {}", symbol="ExecFoo",
            metadata={"relative_path": "src/x.cpp"}, score=0.03, lexical_rank=1, vector_rank=2,
        )]

    def format_context(self, hits, **kwargs):
        return "[RAG-1] [源码: src/x.cpp::ExecFoo]\nvoid ExecFoo() {}"


def test_sentence_transformer_provider_is_lazy():
    provider = SentenceTransformerEmbeddingProvider({"model": "Qwen/Qwen3-Embedding-0.6B", "dimension": 512})
    assert provider.dimension == 512
    assert "Qwen3-Embedding-0.6B" in provider.signature
    assert provider._model is None


def test_code_chunking_keeps_symbol_and_line_refs():
    text = """// helper\nint helper(int x) {\n  return x + 1;\n}\n\nvoid ExecReScanStream() {\n  send_params();\n}\n"""
    chunks = chunk_document(text, "stream.cpp", max_chars=2000)
    symbols = {c.symbol for c in chunks}
    assert "helper" in symbols
    assert "ExecReScanStream" in symbols
    target = next(c for c in chunks if c.symbol == "ExecReScanStream")
    assert target.start_ref.startswith("L") and target.end_ref.startswith("L")


def make_rag(tmp_path: Path):
    store = Store(tmp_path / "rag.sqlite")
    memory = MemoryManager(store, {})
    rag = RAGService(
        store,
        {
            "embedding": {"provider": "hash", "dimension": 64},
            "vector": {"backend": "python", "allow_python_fallback": True},
            "retrieval": {"lexical_top_k": 20, "vector_top_k": 20, "final_top_k": 8},
        },
        provider=HashEmbeddingProvider(64),
    )
    return store, memory, rag


def test_memory_sync_embedding_and_hybrid_search_is_cross_conversation_by_default(tmp_path: Path):
    store, memory, rag = make_rag(tmp_path)
    memory.remember("global", "Stream PBE", "savedserializedPlan caches the generic serialized plan", tags="stream pbe")
    memory.remember("conversation:welink:group:g1", "Private G1", "sideConsumer sends round parameters", tags="sideConsumer")
    memory.remember("conversation:welink:group:g2", "Private G2", "secret_other_conversation_token", tags="private")

    sync = rag.sync_memory()
    assert sync["documents"] == 3
    embedded = rag.embed_pending(limit=20)
    assert embedded["embedded"] == 3
    assert embedded["backend"] == "python"

    hits = rag.search("sideConsumer round parameters", conversation_id="welink:group:g1", limit=6)
    assert hits
    assert any("sideConsumer" in h.content for h in hits)
    assert any(h.vector_rank is not None for h in hits)

    cross = rag.search("secret_other_conversation_token", conversation_id="welink:group:g1", limit=6)
    assert any("secret_other_conversation_token" in h.content for h in cross)


def test_memory_rag_can_restore_v1110_conversation_isolation(tmp_path: Path):
    store = Store(tmp_path / "isolated.sqlite")
    memory = MemoryManager(store, {"cross_conversation_retrieval": False})
    rag = RAGService(store, {
        "embedding": {"provider": "hash", "dimension": 64},
        "vector": {"backend": "python"},
        "retrieval": {"memory_scope": "conversation", "final_top_k": 8},
    }, provider=HashEmbeddingProvider(64))
    memory.remember("global", "Global", "shared token")
    memory.remember("conversation:welink:group:g1", "G1", "alpha private")
    memory.remember("conversation:welink:group:g2", "G2", "secret_other_conversation_token")
    rag.sync_memory(); rag.embed_pending(limit=20)
    hits = rag.search("secret_other_conversation_token", conversation_id="welink:group:g1", source_types=["memory"], limit=6)
    assert all("secret_other_conversation_token" not in h.content for h in hits)
    assert memory.relevant_scopes("welink:group:g1") == ["global", "conversation:welink:group:g1"]


def test_workspace_sync_uses_semantic_code_chunks_and_source_markers(tmp_path: Path):
    store, _memory, rag = make_rag(tmp_path)
    root = tmp_path / "example_repo"
    src = root / "stream.cpp"
    src.parent.mkdir(parents=True)
    src.write_text("void ExecReScanStream() {\n  send_params();\n}\n", encoding="utf-8")
    digest = __import__("hashlib").sha256(src.read_bytes()).hexdigest()
    store.execute(
        "INSERT INTO workspace_files(path,project_root,relative_path,size,mtime_ns,content_hash,file_kind) VALUES (?,?,?,?,?,?,?)",
        (str(src), str(root), "stream.cpp", src.stat().st_size, src.stat().st_mtime_ns, digest, "cpp"),
    )
    store.execute(
        "INSERT INTO workspace_chunks(path,project_root,chunk_index,content) VALUES (?,?,0,?)",
        (str(src), str(root), src.read_text(encoding="utf-8")),
    )
    result = rag.sync_workspace()
    assert result["documents"] == 1
    rag.embed_pending(limit=20)
    hits = rag.search("ExecReScanStream send params", conversation_id="welink:group:g", limit=4)
    assert hits
    code_hit = next(h for h in hits if h.source_type == "code")
    assert code_hit.symbol == "ExecReScanStream"
    assert code_hit.source_marker().startswith("[源码: stream.cpp::ExecReScanStream")


@pytest.mark.asyncio
async def test_agent_can_request_active_rag_without_direct_sql(tmp_path: Path):
    store = Store(tmp_path / "agent.sqlite")
    conv = ConversationManager(store)
    manager = AgentManager({"command": "codeagent", "max_concurrent": 1}, tmp_path, conv, MemoryManager(store, {}))
    fake_rag = ActiveOnlyFakeRAG()
    manager.set_rag_service(fake_rag)
    manager.backend = CaptureBackend([
        '<WORKBOT_RAG>{"query":"ExecFoo rescan path","sources":["code"],"top_k":8}</WORKBOT_RAG>',
        '结论：ExecFoo 是候选入口。 [源码: src/x.cpp::ExecFoo]',
    ])

    result = await manager.answer("welink:group:g", "这个 rescan 路径怎么走？")
    assert result.text.endswith("[源码: src/x.cpp::ExecFoo]")
    assert len(fake_rag.calls) == 1
    assert fake_rag.calls[0][0] == "ExecFoo rescan path"
    assert len(manager.backend.calls) == 2
    assert "WorkBot completed the read-only Hybrid RAG lookup" in manager.backend.calls[1][0]
    assert "Do not query the WorkBot SQLite DB directly" in manager._context_prompt("welink:group:g", "x")


def test_rag_status_reports_canonical_layer_without_optional_dependencies(tmp_path: Path):
    store, _memory, rag = make_rag(tmp_path)
    status = rag.status()
    assert status["enabled"] is True
    assert status["fts"] in {True, False}
    assert status["embedding_provider"] == "hash-v1:64"
    assert status["configured_dimension"] == 64


class PreferSecondReranker:
    signature = "fake-reranker-v1"

    def score(self, query: str, passages):
        return [1.0 if "SECOND_WINS" in p else 0.0 for p in passages]


def test_generic_future_sources_get_source_aware_markers_and_vectors(tmp_path: Path):
    store, _memory, rag = make_rag(tmp_path)
    rag.upsert_text_document(
        source_type="wiki",
        source_key="wiki:stream-pbe-design",
        title="Stream PBE 设计说明",
        text="Sideway Channel 用于在执行轮次之间传递参数。",
        source_uri="https://wiki.example/stream-pbe",
    )
    rag.upsert_text_document(
        source_type="manual",
        source_key="manual:guc",
        title="GaussDB 产品手册",
        text="# GUC 参数\nenable_stream_pbe 控制 Stream PBE。",
        source_uri=r"C:\example\workspace\manuals\guc.md",
    )
    result = rag.embed_pending(limit=20)
    assert result["embedded"] == 2

    wiki_hits = rag.search("Sideway Channel 参数", source_types=["wiki"], limit=3)
    assert wiki_hits and wiki_hits[0].source_marker() == "[Wiki: Stream PBE 设计说明 | https://wiki.example/stream-pbe]"

    manual_hits = rag.search("enable_stream_pbe", source_types=["manual"], limit=3)
    assert manual_hits
    assert manual_hits[0].source_marker().startswith("[产品手册: GaussDB 产品手册")
    assert manual_hits[0].vector_rank is not None


def test_optional_reranker_can_reorder_rrf_candidates(tmp_path: Path):
    store = Store(tmp_path / "rerank.sqlite")
    rag = RAGService(
        store,
        {
            "embedding": {"provider": "hash", "dimension": 64},
            "vector": {"backend": "python"},
            "retrieval": {"lexical_top_k": 10, "vector_top_k": 10, "final_top_k": 2},
            "reranker": {"enabled": True, "candidate_k": 4},
        },
        provider=HashEmbeddingProvider(64),
        reranker=PreferSecondReranker(),
    )
    rag.upsert_text_document(
        source_type="web", source_key="web:first", title="First",
        text="stream parameter rescan common candidate", source_uri="https://example/first",
    )
    rag.upsert_text_document(
        source_type="web", source_key="web:second", title="Second",
        text="stream parameter rescan SECOND_WINS candidate", source_uri="https://example/second",
    )
    rag.embed_pending(limit=10)
    hits = rag.search("stream parameter rescan", source_types=["web"], limit=2)
    assert len(hits) == 2
    assert "SECOND_WINS" in hits[0].content
    assert hits[0].rerank_score == 1.0
    assert hits[0].source_marker() == "[Web: Second | https://example/second]"
