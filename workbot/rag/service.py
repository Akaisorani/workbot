from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from workbot.rag.chunking import CHUNKER_VERSION, ChunkSpec, chunk_document, is_code_path
from workbot.rag.embedding import EmbeddingProvider, EmbeddingUnavailable, build_embedding_provider
from workbot.rag.store import RAGStore, VectorIndex
from workbot.rag.reranker import build_reranker
from workbot.storage.sqlite import Store

log = logging.getLogger(__name__)


_DEFAULT_CFG = {
    "enabled": True,
    "passive_enabled": True,
    "active_enabled": True,
    "embedding": {
        "provider": "sentence-transformers",
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "dimension": 512,
        "revision": "main",
        "device": "auto",
        "batch_size": 16,
        "query_prompt_name": "query",
        "cpu_threads": 0,
    },
    "vector": {
        "backend": "sqlite-vec",
        "allow_python_fallback": True,
    },
    "index": {
        "auto_sync": True,
        "auto_embed": True,
        "batch_chunks": 24,
        "job_batch_chunks": 32,
        "chunk_chars": 4500,
    },
    "retrieval": {
        "lexical_top_k": 30,
        "vector_top_k": 30,
        "final_top_k": 8,
        "rrf_k": 60,
        "lexical_weight": 1.0,
        "vector_weight": 1.2,
        "max_chunks_per_document": 2,
        "memory_scope": "all",
    },
    "reranker": {
        "enabled": False,
        "provider": "sentence-transformers",
        "model": "Qwen/Qwen3-Reranker-0.6B",
        "revision": "main",
        "device": "auto",
        "batch_size": 8,
        "candidate_k": 16,
    },
    "active": {
        "max_rounds": 2,
        "max_top_k": 16,
    },
}


def _deep_merge(base: dict, override: dict | None) -> dict:
    out = {}
    for key, value in base.items():
        out[key] = dict(value) if isinstance(value, dict) else value
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(slots=True)
class RAGHit:
    chunk_id: int
    document_id: int
    source_type: str
    source_key: str
    namespace: str
    title: str
    source_uri: str
    content: str
    section: str = ""
    symbol: str = ""
    start_ref: str = ""
    end_ref: str = ""
    score: float = 0.0
    lexical_rank: int | None = None
    vector_rank: int | None = None
    distance: float | None = None
    rerank_score: float | None = None
    metadata: dict | None = None

    def source_marker(self) -> str:
        meta = dict(self.metadata or {})
        if self.source_type == "memory":
            mid = meta.get("memory_id") or self.source_key.removeprefix("memory:")
            return f"[memory:{mid} {self.namespace}]"
        if self.source_type == "code":
            path = meta.get("relative_path") or self.source_uri or self.title
            suffix = f"::{self.symbol}" if self.symbol else ""
            refs = ""
            if self.start_ref:
                refs = f" ({self.start_ref}-{self.end_ref or self.start_ref})"
            return f"[源码: {path}{suffix}{refs}]"
        path = self.source_uri or self.title
        section = f" -> {self.section}" if self.section else ""
        if self.source_type in {"manual", "product-manual", "product_manual"}:
            return f"[产品手册: {self.title or path}{section}]"
        if self.source_type == "wiki":
            return f"[Wiki: {self.title or path} | {self.source_uri or path}]"
        if self.source_type in {"web", "w3"}:
            return f"[Web: {self.title or path} | {self.source_uri or path}]"
        return f"[文件: {path}{section}]"


class RAGService:
    """Unified local RAG over WorkBot memory/workspace data.

    The canonical text/chunk layer lives in the existing WorkBot SQLite DB.  A
    sqlite-vec exact-KNN index is used when the extension is installed; a small
    Python cosine fallback keeps migrations/tests usable without making WorkBot
    startup depend on optional ML packages.
    """

    def __init__(self, store: Store, cfg: dict | None = None, *, provider: EmbeddingProvider | None = None, reranker=None):
        self.cfg = _deep_merge(_DEFAULT_CFG, cfg or {})
        self.enabled = bool(self.cfg.get("enabled", True))
        vector_cfg = dict(self.cfg.get("vector") or {})
        self.rag_store = RAGStore(
            store,
            allow_python_fallback=bool(vector_cfg.get("allow_python_fallback", True)),
            preferred_backend=str(vector_cfg.get("backend") or "sqlite-vec"),
        )
        self.store = store
        self.provider = provider or build_embedding_provider(dict(self.cfg.get("embedding") or {}))
        self.reranker = reranker if reranker is not None else build_reranker(dict(self.cfg.get("reranker") or {}))
        self._last_embedding_error = ""
        self._last_reranker_error = ""
        self._sync_lock = asyncio.Lock()
        self._embed_task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._embed_stop = threading.Event()
        self._sync_stop = threading.Event()
        self._embed_job: dict = {"state": "idle"}
        self._sync_job: dict = {"state": "idle"}

    @property
    def passive_enabled(self) -> bool:
        return self.enabled and bool(self.cfg.get("passive_enabled", True))

    @property
    def active_enabled(self) -> bool:
        return self.enabled and bool(self.cfg.get("active_enabled", True))

    @property
    def max_active_rounds(self) -> int:
        return max(0, int((self.cfg.get("active") or {}).get("max_rounds", 2)))

    @staticmethod
    def _decode_local_file(path: Path) -> str | None:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in raw[:8192]:
            return None
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _enrich_chunk(title: str, spec: ChunkSpec) -> str:
        headers = [f"Document: {title}"]
        if spec.section:
            headers.append(f"Section: {spec.section}")
        if spec.symbol:
            headers.append(f"Symbol: {spec.symbol}")
        return "\n".join(headers) + "\n\n" + spec.content.strip()

    def sync_memory(self, *, progress=None, stop_event: threading.Event | None = None) -> dict:
        if not self.enabled:
            return {"documents": 0, "changed": 0, "removed": 0}
        rows = self.store.query_all(
            "SELECT id,scope,memory_kind,title,content,tags,source,confidence,evidence,updated_at FROM memory_items ORDER BY id"
        )
        keys: set[str] = set()
        changed = 0
        documents_changed = 0
        processed = 0
        for r in rows:
            if stop_event is not None and stop_event.is_set():
                break
            mid = int(r["id"])
            key = f"memory:{mid}"
            keys.add(key)
            content = str(r["content"] or "").strip()
            title = str(r["title"] or "WorkBot memory")
            body = f"Memory: {title}\nScope: {r['scope']}\nKind: {r['memory_kind']}\n\n{content}"
            digest = hashlib.sha256(
                (CHUNKER_VERSION + "\n" + title + "\n" + content + "\n" + str(r["tags"] or "") + "\n" + str(r["evidence"] or "")).encode("utf-8")
            ).hexdigest()
            doc_id, doc_changed = self.rag_store.upsert_document(
                source_type="memory",
                source_key=key,
                namespace=str(r["scope"] or "global"),
                title=title,
                source_uri=key,
                content_hash=digest,
                metadata={
                    "memory_id": mid,
                    "scope": str(r["scope"] or ""),
                    "memory_kind": str(r["memory_kind"] or "fact"),
                    "tags": str(r["tags"] or ""),
                    "source": str(r["source"] or ""),
                    "confidence": float(r["confidence"] or 0),
                },
            )
            if doc_changed:
                documents_changed += 1
                changed += self.rag_store.replace_chunks(doc_id, [{
                    "content": body,
                    "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "metadata": {"memory_id": mid},
                }])
            processed += 1
            if progress and (processed % 100 == 0 or processed == len(rows)):
                progress({"phase": "memory", "documents_processed": processed, "documents_total": len(rows), "documents_changed": documents_changed, "chunks_upserted": changed})
        removed = 0 if (stop_event is not None and stop_event.is_set()) else self.rag_store.delete_missing_documents(["memory"], keys, source_key_prefix="memory:")
        return {"documents": len(keys), "processed": processed, "documents_changed": documents_changed, "chunks_upserted": changed, "changed": changed, "removed": removed, "stopped": bool(stop_event is not None and stop_event.is_set())}

    def _workspace_specs_from_existing_chunks(self, path: str) -> list[ChunkSpec]:
        rows = self.store.query_all(
            "SELECT chunk_index,content FROM workspace_chunks WHERE path=? ORDER BY chunk_index", (path,)
        )
        return [ChunkSpec(content=str(r["content"] or "")) for r in rows if str(r["content"] or "").strip()]

    def sync_workspace(self, *, progress=None, stop_event: threading.Event | None = None) -> dict:
        if not self.enabled:
            return {"documents": 0, "changed": 0, "removed": 0}
        rows = self.store.query_all(
            "SELECT path,project_root,relative_path,content_hash,file_kind,size,mtime_ns FROM workspace_files ORDER BY path"
        )
        keys: set[str] = set()
        changed = 0
        documents_changed = 0
        processed = 0
        max_chars = max(1000, int((self.cfg.get("index") or {}).get("chunk_chars", 4500)))
        for r in rows:
            if stop_event is not None and stop_event.is_set():
                break
            path = str(r["path"])
            key = f"workspace:{path}"
            keys.add(key)
            raw_digest = str(r["content_hash"] or "") or hashlib.sha256(path.encode("utf-8")).hexdigest()
            digest = hashlib.sha256((CHUNKER_VERSION + ":" + raw_digest).encode("utf-8")).hexdigest()
            source_type = "code" if is_code_path(path) else "workspace"
            relative = str(r["relative_path"] or path)
            doc_id, doc_changed = self.rag_store.upsert_document(
                source_type=source_type,
                source_key=key,
                namespace=str(r["project_root"] or ""),
                title=relative,
                source_uri=path,
                content_hash=digest,
                metadata={
                    "project_root": str(r["project_root"] or ""),
                    "relative_path": relative,
                    "file_kind": str(r["file_kind"] or ""),
                    "size": int(r["size"] or 0),
                    "mtime_ns": int(r["mtime_ns"] or 0),
                },
            )
            if not doc_changed:
                continue
            specs: list[ChunkSpec] = []
            if not path.startswith("ssh://"):
                local_path = Path(path)
                text = self._decode_local_file(local_path) if local_path.exists() else None
                if text:
                    specs = chunk_document(text, path, max_chars=max_chars)
            if not specs:
                specs = self._workspace_specs_from_existing_chunks(path)
            payload = []
            for spec in specs:
                content = self._enrich_chunk(relative, spec)
                payload.append({
                    "content": content,
                    "section": spec.section,
                    "symbol": spec.symbol,
                    "start_ref": spec.start_ref,
                    "end_ref": spec.end_ref,
                    "metadata": {"relative_path": relative, "project_root": str(r["project_root"] or "")},
                })
            changed += self.rag_store.replace_chunks(doc_id, payload)
        removed = self.rag_store.delete_missing_documents(["workspace", "code"], keys, source_key_prefix="workspace:")
        return {"documents": len(keys), "changed": changed, "removed": removed}

    def upsert_text_document(self, *, source_type: str, source_key: str, title: str, text: str,
                             namespace: str = "", source_uri: str = "", metadata: dict | None = None) -> dict:
        """Index one future/connector document into the canonical RAG layer.

        This API is intentionally source-agnostic so later Wiki/manual/web/mail
        caches can reuse the same chunk/vector infrastructure without changing
        the memory/workspace schemas. Callers remain responsible for access
        control and for choosing a non-colliding ``source_key``.
        """
        source_type = str(source_type or "knowledge").strip() or "knowledge"
        source_key = str(source_key or "").strip()
        if not source_key:
            raise ValueError("source_key is required")
        title = str(title or source_key).strip()
        text = str(text or "")
        digest = hashlib.sha256((CHUNKER_VERSION + "\n" + text).encode("utf-8")).hexdigest()
        doc_id, changed = self.rag_store.upsert_document(
            source_type=source_type, source_key=source_key, namespace=str(namespace or ""),
            title=title, source_uri=str(source_uri or ""), content_hash=digest,
            metadata={**(metadata or {}), "chunker_version": CHUNKER_VERSION},
        )
        chunk_changes = 0
        if changed:
            specs = chunk_document(text, source_uri or title, max_chars=max(1000, int((self.cfg.get("index") or {}).get("chunk_chars", 4500))))
            chunk_changes = self.rag_store.replace_chunks(doc_id, [{
                "content": self._enrich_chunk(title, spec),
                "section": spec.section, "symbol": spec.symbol,
                "start_ref": spec.start_ref, "end_ref": spec.end_ref,
                "metadata": metadata or {},
            } for spec in specs])
        return {"document_id": doc_id, "changed": bool(changed), "chunk_changes": chunk_changes}

    def sync_sources(self, *, progress=None, stop_event: threading.Event | None = None) -> dict:
        if not self.enabled:
            return {"enabled": False}
        memory = self.sync_memory(progress=progress, stop_event=stop_event)
        if stop_event is not None and stop_event.is_set():
            return {"enabled": True, "memory": memory, "workspace": {"stopped": True}}
        workspace = self.sync_workspace(progress=progress, stop_event=stop_event)
        return {"enabled": True, "memory": memory, "workspace": workspace}

    def _ensure_index(self) -> VectorIndex:
        return self.rag_store.ensure_vector_index(self.provider.signature, self.provider.dimension)

    def embed_pending(self, *, limit: int | None = None) -> dict:
        if not self.enabled:
            return {"embedded": 0, "backend": "disabled"}
        index = self._ensure_index()
        if index.backend == "unavailable":
            return {"embedded": 0, "backend": "unavailable"}
        batch = max(1, int(limit or (self.cfg.get("index") or {}).get("batch_chunks", 24)))
        rows = self.rag_store.pending_chunks(index, batch)
        if not rows:
            self._last_embedding_error = ""
            return {"embedded": 0, "backend": index.backend, "index": index.index_name}
        texts = [str(r["content"] or "") for r in rows]
        try:
            vectors = self.provider.embed_documents(texts)
        except EmbeddingUnavailable as exc:
            self._last_embedding_error = str(exc)
            log.warning("RAG embedding unavailable: %s", exc)
            return {"embedded": 0, "backend": index.backend, "error": str(exc)}
        except Exception as exc:
            self._last_embedding_error = str(exc)
            log.exception("RAG embedding failed")
            return {"embedded": 0, "backend": index.backend, "error": str(exc)}
        items = [
            (
                int(r["id"]), str(r["content_hash"]),
                self.rag_store.partition_key(str(r["source_type"] or ""), str(r["namespace"] or "")),
                vector,
            )
            for r, vector in zip(rows, vectors)
        ]
        self.rag_store.upsert_vectors(index, items)
        self._last_embedding_error = ""
        return {"embedded": len(items), "backend": index.backend, "index": index.index_name}

    @property
    def manual_job_active(self) -> bool:
        return bool((self._embed_task and not self._embed_task.done()) or (self._sync_task and not self._sync_task.done()))

    def _corpus_progress(self) -> dict:
        st = self.rag_store.status()
        chunks = int(st.get("chunks") or 0)
        embedded = int(st.get("embedded_chunks") or 0)
        pending = max(0, chunks - embedded)
        return {"chunks": chunks, "embedded": embedded, "pending": pending,
                "coverage_percent": round((100.0 * embedded / chunks), 2) if chunks else 100.0}

    def embed_job_status(self) -> dict:
        out = dict(self._embed_job)
        out.update(self._corpus_progress())
        return out

    def sync_job_status(self) -> dict:
        return dict(self._sync_job)

    async def start_embed_job(self, *, limit: int | None = None, all_chunks: bool = False) -> dict:
        if self._embed_task and not self._embed_task.done():
            return self.embed_job_status()
        self._embed_stop.clear()
        target = None if all_chunks else max(1, int(limit or (self.cfg.get("index") or {}).get("batch_chunks", 24)))
        self._embed_job = {"state": "running", "target": "all" if target is None else target,
                           "job_embedded": 0, "started_at": time.time(), "elapsed_seconds": 0.0,
                           "chunks_per_second": 0.0, "eta_seconds": None, "last_error": ""}
        self._embed_task = asyncio.create_task(self._run_embed_job(target), name="rag-embed-job")
        return self.embed_job_status()

    async def _run_embed_job(self, target: int | None) -> None:
        started = time.monotonic()
        total = 0
        micro = max(1, int((self.cfg.get("index") or {}).get("job_batch_chunks", 32)))
        try:
            async with self._sync_lock:
                while not self._embed_stop.is_set() and (target is None or total < target):
                    n = micro if target is None else min(micro, target - total)
                    result = await asyncio.to_thread(self.embed_pending, limit=n)
                    got = int(result.get("embedded") or 0)
                    total += got
                    elapsed = max(1e-6, time.monotonic() - started)
                    rate = total / elapsed
                    self._embed_job.update({"job_embedded": total, "elapsed_seconds": round(elapsed, 1),
                                            "chunks_per_second": round(rate, 4), "backend": result.get("backend", ""),
                                            "index": result.get("index", ""), "last_error": result.get("error", "") or ""})
                    progress = self._corpus_progress()
                    remaining_job = progress["pending"] if target is None else max(0, target-total)
                    self._embed_job["eta_seconds"] = round(remaining_job/rate, 1) if rate > 0 and remaining_job else 0.0
                    if got <= 0 or result.get("error"):
                        break
                self._embed_job["state"] = "stopped" if self._embed_stop.is_set() else "completed"
        except asyncio.CancelledError:
            self._embed_job["state"] = "cancelled"
            raise
        except Exception as exc:
            self._embed_job.update({"state": "failed", "last_error": str(exc)})
            log.exception("RAG background embedding job failed")
        finally:
            self._embed_job["elapsed_seconds"] = round(time.monotonic()-started, 1)

    def stop_embed_job(self) -> dict:
        self._embed_stop.set()
        if self._embed_job.get("state") == "running":
            self._embed_job["state"] = "stopping"
        return self.embed_job_status()

    async def start_sync_job(self) -> dict:
        if self._sync_task and not self._sync_task.done():
            return self.sync_job_status()
        self._sync_stop.clear()
        self._sync_job = {"state": "running", "phase": "starting", "started_at": time.time(),
                          "elapsed_seconds": 0.0, "documents_processed": 0, "documents_total": 0,
                          "documents_changed": 0, "chunks_upserted": 0, "last_error": ""}
        self._sync_task = asyncio.create_task(self._run_sync_job(), name="rag-sync-job")
        return self.sync_job_status()

    async def _run_sync_job(self) -> None:
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        def progress(update: dict) -> None:
            def apply():
                self._sync_job.update(update)
                self._sync_job["elapsed_seconds"] = round(time.monotonic()-started, 1)
            loop.call_soon_threadsafe(apply)
        try:
            async with self._sync_lock:
                result = await asyncio.to_thread(self.sync_sources, progress=progress, stop_event=self._sync_stop)
            self._sync_job["result"] = result
            self._sync_job["state"] = "stopped" if self._sync_stop.is_set() else "completed"
        except asyncio.CancelledError:
            self._sync_job["state"] = "cancelled"
            raise
        except Exception as exc:
            self._sync_job.update({"state": "failed", "last_error": str(exc)})
            log.exception("RAG background sync job failed")
        finally:
            self._sync_job["elapsed_seconds"] = round(time.monotonic()-started, 1)

    def stop_sync_job(self) -> dict:
        self._sync_stop.set()
        if self._sync_job.get("state") == "running":
            self._sync_job["state"] = "stopping"
        return self.sync_job_status()

    async def maintenance_once(self) -> bool:
        if not self.enabled or self.manual_job_active:
            return False
        async with self._sync_lock:
            did = False
            if bool((self.cfg.get("index") or {}).get("auto_sync", True)):
                result = await asyncio.to_thread(self.sync_sources)
                did = bool(
                    (result.get("memory") or {}).get("changed")
                    or (result.get("memory") or {}).get("removed")
                    or (result.get("workspace") or {}).get("changed")
                    or (result.get("workspace") or {}).get("removed")
                )
            if bool((self.cfg.get("index") or {}).get("auto_embed", True)):
                embedded = await asyncio.to_thread(self.embed_pending)
                did = did or int(embedded.get("embedded") or 0) > 0
            return did

    def force_reindex(self) -> dict:
        if self.manual_job_active:
            raise RuntimeError("RAG sync/embed job is running; stop it before reindex")
        index = self._ensure_index()
        self.rag_store.reset_index(index)
        self._last_embedding_error = ""
        return {"index": index.index_name, "backend": index.backend}

    @staticmethod
    def _metadata(row: dict) -> dict:
        raw = row.get("metadata_json")
        if not raw:
            return {}
        try:
            obj = json.loads(str(raw))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _allowed(self, row: dict, *, conversation_id: str | None, source_types: set[str] | None) -> bool:
        st = str(row.get("source_type") or "")
        if source_types and st not in source_types:
            return False
        if st == "memory":
            memory_scope = str((self.cfg.get("retrieval") or {}).get("memory_scope", "all") or "all").lower()
            if memory_scope != "conversation":
                return True
            ns = str(row.get("namespace") or "")
            allowed = {"global"}
            if conversation_id:
                allowed.add(f"conversation:{conversation_id}")
            return ns in allowed
        return True

    def search(self, query: str, *, conversation_id: str | None = None, limit: int | None = None,
               source_types: Sequence[str] | None = None) -> list[RAGHit]:
        query = (query or "").strip()
        if not self.enabled or not query:
            return []
        rcfg = dict(self.cfg.get("retrieval") or {})
        final_limit = max(1, int(limit or rcfg.get("final_top_k", 8)))
        lexical_limit = max(final_limit, int(rcfg.get("lexical_top_k", 30)))
        vector_limit = max(final_limit, int(rcfg.get("vector_top_k", 30)))
        wanted = set(str(x) for x in source_types) if source_types else None

        lexical_raw = [r for r in self.rag_store.lexical_search(query, lexical_limit)
                       if self._allowed(r, conversation_id=conversation_id, source_types=wanted)]
        vector_raw: list[dict] = []
        index = self.rag_store.active_vector_index()
        if index:
            ready = self.store.query_one(
                "SELECT COUNT(*) AS n FROM rag_chunk_embeddings WHERE index_name=?", (index.index_name,)
            )
            if ready and int(ready["n"] or 0) > 0:
                try:
                    qv = self.provider.embed_query(query)
                    if wanted is not None:
                        requested_types = set(wanted)
                    else:
                        type_rows = self.store.query_all("SELECT DISTINCT source_type FROM rag_documents")
                        requested_types = {str(r["source_type"]) for r in type_rows} or {"memory", "workspace", "code"}
                    partitions: list[str] = []
                    if "memory" in requested_types:
                        memory_scope = str(rcfg.get("memory_scope", "all") or "all").lower()
                        if memory_scope == "conversation":
                            partitions.append(self.rag_store.partition_key("memory", "global"))
                            if conversation_id:
                                partitions.append(self.rag_store.partition_key("memory", f"conversation:{conversation_id}"))
                        else:
                            ns_rows = self.store.query_all("SELECT DISTINCT namespace FROM rag_documents WHERE source_type='memory'")
                            for ns_row in ns_rows:
                                partitions.append(self.rag_store.partition_key("memory", str(ns_row["namespace"] or "global")))
                    if "workspace" in requested_types:
                        partitions.append(self.rag_store.partition_key("workspace", ""))
                    if "code" in requested_types:
                        partitions.append(self.rag_store.partition_key("code", ""))
                    for custom_type in sorted(requested_types - {"memory", "workspace", "code"}):
                        partitions.append(self.rag_store.partition_key(custom_type, ""))
                    vector_raw = [r for r in self.rag_store.vector_search(index, qv, vector_limit * 2, partition_keys=partitions or None)
                                  if self._allowed(r, conversation_id=conversation_id, source_types=wanted)][:vector_limit]
                    self._last_embedding_error = ""
                except Exception as exc:
                    self._last_embedding_error = str(exc)
                    log.warning("RAG query embedding/vector search unavailable: %s", exc)

        rrf_k = max(1, int(rcfg.get("rrf_k", 60)))
        lw = float(rcfg.get("lexical_weight", 1.0))
        vw = float(rcfg.get("vector_weight", 1.2))
        merged: dict[int, dict] = {}
        for rank, row in enumerate(lexical_raw, start=1):
            cid = int(row["chunk_id"])
            entry = merged.setdefault(cid, dict(row))
            entry["_score"] = float(entry.get("_score", 0.0)) + lw / (rrf_k + rank)
            entry["_lexical_rank"] = rank
        for rank, row in enumerate(vector_raw, start=1):
            cid = int(row["chunk_id"])
            entry = merged.setdefault(cid, dict(row))
            for key, value in row.items():
                entry.setdefault(key, value)
            entry["_score"] = float(entry.get("_score", 0.0)) + vw / (rrf_k + rank)
            entry["_vector_rank"] = rank
            entry["distance"] = row.get("distance")

        # Exact identifier/path hits deserve a small deterministic boost. This
        # preserves FTS5's strength on source symbols/GUCs while semantic search
        # handles paraphrases.
        qlow = query.lower()
        for entry in merged.values():
            hay = " ".join([
                str(entry.get("title") or ""), str(entry.get("source_uri") or ""),
                str(entry.get("section") or ""), str(entry.get("symbol") or ""),
            ]).lower()
            if qlow and qlow in hay:
                entry["_score"] = float(entry.get("_score", 0.0)) + 0.02

        ordered = sorted(merged.values(), key=lambda x: (-float(x.get("_score", 0.0)), int(x.get("chunk_id", 0))))
        if self.reranker is not None and ordered:
            rerank_cfg = dict(self.cfg.get("reranker") or {})
            candidate_k = min(len(ordered), max(final_limit, int(rerank_cfg.get("candidate_k", 16))))
            candidates = ordered[:candidate_k]
            try:
                rerank_scores = self.reranker.score(query, [str(x.get("content") or "") for x in candidates])
                for item, rscore in zip(candidates, rerank_scores):
                    item["_rerank_score"] = float(rscore)
                candidates.sort(key=lambda x: (-float(x.get("_rerank_score", -1e30)), -float(x.get("_score", 0.0))))
                ordered = candidates + ordered[candidate_k:]
                self._last_reranker_error = ""
            except Exception as exc:
                self._last_reranker_error = str(exc)
                log.warning("RAG reranker unavailable; keeping RRF order: %s", exc)
        per_doc = max(1, int(rcfg.get("max_chunks_per_document", 2)))
        doc_counts: dict[int, int] = {}
        hits: list[RAGHit] = []
        for r in ordered:
            doc_id = int(r["document_id"])
            if doc_counts.get(doc_id, 0) >= per_doc:
                continue
            doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1
            hits.append(RAGHit(
                chunk_id=int(r["chunk_id"]), document_id=doc_id,
                source_type=str(r.get("source_type") or ""), source_key=str(r.get("source_key") or ""),
                namespace=str(r.get("namespace") or ""), title=str(r.get("title") or ""),
                source_uri=str(r.get("source_uri") or ""), content=str(r.get("content") or ""),
                section=str(r.get("section") or ""), symbol=str(r.get("symbol") or ""),
                start_ref=str(r.get("start_ref") or ""), end_ref=str(r.get("end_ref") or ""),
                score=float(r.get("_score", 0.0)), lexical_rank=r.get("_lexical_rank"),
                vector_rank=r.get("_vector_rank"), distance=(float(r["distance"]) if r.get("distance") is not None else None),
                rerank_score=(float(r["_rerank_score"]) if r.get("_rerank_score") is not None else None),
                metadata=self._metadata(r),
            ))
            if len(hits) >= final_limit:
                break
        return hits

    def format_context(self, hits: Sequence[RAGHit], *, max_chars: int = 14000) -> str:
        if not hits:
            return ""
        parts: list[str] = []
        total = 0
        for i, hit in enumerate(hits, start=1):
            score_info = f"score={hit.score:.4f}"
            if hit.lexical_rank:
                score_info += f" fts_rank={hit.lexical_rank}"
            if hit.vector_rank:
                score_info += f" vec_rank={hit.vector_rank}"
            if hit.rerank_score is not None:
                score_info += f" rerank={hit.rerank_score:.4f}"
            header = f"[RAG-{i}] {hit.source_marker()} {score_info}"
            body = hit.content.strip()
            item = header + "\n" + body
            if total + len(item) > max_chars and parts:
                break
            parts.append(item)
            total += len(item) + 2
        return "\n\n".join(parts)

    def status(self) -> dict:
        base = self.rag_store.status()
        import importlib.util
        base.update({
            "enabled": self.enabled,
            "passive_enabled": self.passive_enabled,
            "active_enabled": self.active_enabled,
            "embedding_provider": self.provider.signature,
            "configured_dimension": self.provider.dimension,
            "chunker_version": CHUNKER_VERSION,
            "sentence_transformers_installed": importlib.util.find_spec("sentence_transformers") is not None,
            "sqlite_vec_installed": importlib.util.find_spec("sqlite_vec") is not None,
            "reranker": getattr(self.reranker, "signature", "disabled") if self.reranker is not None else "disabled",
            "last_embedding_error": self._last_embedding_error,
            "last_reranker_error": self._last_reranker_error,
            "pending_chunks": max(0, int(base.get("chunks", 0)) - int(base.get("embedded_chunks", 0))),
            "coverage_percent": round((100.0 * int(base.get("embedded_chunks", 0)) / int(base.get("chunks", 1))), 2) if int(base.get("chunks", 0)) else 100.0,
            "embedding_batch_size": int(getattr(self.provider, "batch_size", 0) or 0),
            "embedding_device": str(getattr(self.provider, "device", "unknown")),
            "embedding_cpu_threads": int(getattr(self.provider, "cpu_threads", 0) or 0),
            "memory_scope": str((self.cfg.get("retrieval") or {}).get("memory_scope", "all")),
            "embed_job": self.embed_job_status(),
            "sync_job": self.sync_job_status(),
        })
        return base
