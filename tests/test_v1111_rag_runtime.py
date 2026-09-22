from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from workbot.main import WorkBot
from workbot.rag.embedding import HashEmbeddingProvider
from workbot.rag.service import RAGService
from workbot.storage.sqlite import Store

OWNER = "owner-account"


class SlowHash(HashEmbeddingProvider):
    def embed_documents(self, texts):
        time.sleep(0.04)
        return super().embed_documents(texts)


@pytest.mark.asyncio
async def test_numeric_embed_runs_as_microbatched_background_job(tmp_path: Path):
    store = Store(tmp_path / "rag.sqlite")
    rag = RAGService(store, {
        "embedding": {"provider": "hash", "dimension": 32},
        "vector": {"backend": "python"},
        "index": {"job_batch_chunks": 2},
    }, provider=SlowHash(32))
    for i in range(8):
        rag.upsert_text_document(source_type="workspace", source_key=f"d:{i}", title=f"d{i}", text=f"stream pbe {i}")

    first = await rag.start_embed_job(limit=5)
    assert first["state"] == "running"
    await rag._embed_task
    status = rag.embed_job_status()
    assert status["state"] == "completed"
    assert status["job_embedded"] == 5
    assert status["embedded"] == 5
    assert status["pending"] == 3
    assert status["chunks_per_second"] > 0


@pytest.mark.asyncio
async def test_embed_stop_is_cooperative_between_microbatches(tmp_path: Path):
    store = Store(tmp_path / "stop.sqlite")
    rag = RAGService(store, {
        "embedding": {"provider": "hash", "dimension": 32},
        "vector": {"backend": "python"},
        "index": {"job_batch_chunks": 1},
    }, provider=SlowHash(32))
    for i in range(10):
        rag.upsert_text_document(source_type="workspace", source_key=f"d:{i}", title=f"d{i}", text=f"body {i}")
    await rag.start_embed_job(all_chunks=True)
    await asyncio.sleep(0.01)
    stopped = rag.stop_embed_job()
    assert stopped["state"] in {"stopping", "stopped"}
    await rag._embed_task
    status = rag.embed_job_status()
    assert status["state"] == "stopped"
    assert 0 < status["job_embedded"] < 10


def _make_bot(tmp_path: Path) -> WorkBot:
    (tmp_path / "config").mkdir(exist_ok=True)
    cfg = {
        "database": "state/workbot.db",
        "im": {"cli": "welink-cli", "groups": [], "bootstrap_from_latest": True,
               "intent": {"enabled": True, "require_alias": True, "bot_aliases": ["bot"]}},
        "ingestion_policy": {"default": "allow"},
        "reply_policy": {"default": "allow"},
        "execution_policy": {"default": "deny", "allow_senders": [OWNER]},
        "agent": {"command": "codeagent"}, "nodes": {},
        "rpc": {"enabled": False, "windows_read_roots": [str(tmp_path)]},
        "workspace_knowledge": {"enabled": False},
        "rag": {"enabled": False},
    }
    cp = tmp_path / "config" / "local.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    return WorkBot(cp)


def test_single_execution_allow_sender_is_inferred_as_private_self_account(tmp_path: Path):
    bot = _make_bot(tmp_path)
    assert bot.im.self_accounts == {OWNER}
    mine = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {"chatType": 0, "isMine": True, "receiverAccount": "peer", "recentOwner": "peer",
                 "userAccount": "unstable-hook-id", "showText": "/status", "serverSendTime": "1", "id": "m1"},
    }
    msg = bot.im.normalize_push_event(mine)
    assert msg is not None and msg.from_self is True
    assert msg.sender_id == OWNER
    assert bot._execution_allowed(msg) is True


def test_peer_private_message_does_not_gain_operator_execution(tmp_path: Path):
    bot = _make_bot(tmp_path)
    peer = {
        "type": "weLinkMessage", "func": "receiveIMMessage",
        "data": {"chatType": 0, "isMine": False, "receiverAccount": OWNER, "recentOwner": "peer",
                 "userAccount": "peer", "showText": "/status", "serverSendTime": "2", "id": "m2"},
    }
    msg = bot.im.normalize_push_event(peer)
    assert msg is not None and msg.from_self is False
    assert msg.sender_id == "peer"
    assert bot._execution_allowed(msg) is False


def test_memory_manager_defaults_to_all_scopes_and_can_opt_out(tmp_path: Path):
    from workbot.memory.manager import MemoryManager
    store = Store(tmp_path / "memory.sqlite")
    assert MemoryManager(store, {}).relevant_scopes("welink:group:g1") == []
    strict = MemoryManager(store, {"cross_conversation_retrieval": False})
    assert strict.relevant_scopes("welink:group:g1") == ["global", "conversation:welink:group:g1"]
