from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
from dataclasses import dataclass
from typing import Iterable, Sequence

from workbot.storage.sqlite import Store


RAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    source_uri TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_rag_documents_type ON rag_documents(source_type, namespace, updated_at);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    section TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    start_ref TEXT NOT NULL DEFAULT '',
    end_ref TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc ON rag_chunks(document_id, ordinal);

CREATE TABLE IF NOT EXISTS rag_vector_indexes (
    index_name TEXT PRIMARY KEY,
    provider_signature TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    distance_metric TEXT NOT NULL DEFAULT 'cosine',
    backend TEXT NOT NULL DEFAULT 'unknown',
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS rag_chunk_embeddings (
    chunk_id INTEGER NOT NULL REFERENCES rag_chunks(id) ON DELETE CASCADE,
    index_name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedded_at INTEGER NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY(chunk_id, index_name)
);
CREATE INDEX IF NOT EXISTS idx_rag_embeddings_index ON rag_chunk_embeddings(index_name, embedded_at);

CREATE TABLE IF NOT EXISTS rag_vector_fallback (
    chunk_id INTEGER NOT NULL REFERENCES rag_chunks(id) ON DELETE CASCADE,
    index_name TEXT NOT NULL,
    embedding BLOB NOT NULL,
    PRIMARY KEY(chunk_id, index_name)
);
"""


@dataclass(slots=True)
class VectorIndex:
    index_name: str
    dimension: int
    backend: str
    provider_signature: str


def serialize_f32(values: Sequence[float]) -> bytes:
    return struct.pack(f"={len(values)}f", *[float(x) for x in values])


def deserialize_f32(blob: bytes, dimension: int) -> list[float]:
    return list(struct.unpack(f"={dimension}f", blob))


class RAGStore:
    def __init__(self, store: Store, *, allow_python_fallback: bool = True, preferred_backend: str = "sqlite-vec"):
        self.store = store
        self.allow_python_fallback = bool(allow_python_fallback)
        self.preferred_backend = str(preferred_backend or "sqlite-vec").strip().lower()
        self.fts_available = False
        self.sqlite_vec_available = False
        self.sqlite_vec_version = ""
        self._init_schema()

    def _init_schema(self) -> None:
        with self.store.transaction() as conn:
            conn.executescript(RAG_SCHEMA)
            try:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
                        content, section, symbol,
                        content='rag_chunks', content_rowid='id',
                        tokenize='unicode61'
                    );
                    CREATE TRIGGER IF NOT EXISTS rag_ai AFTER INSERT ON rag_chunks BEGIN
                      INSERT INTO rag_fts(rowid,content,section,symbol)
                      VALUES (new.id,new.content,new.section,new.symbol);
                    END;
                    CREATE TRIGGER IF NOT EXISTS rag_ad AFTER DELETE ON rag_chunks BEGIN
                      INSERT INTO rag_fts(rag_fts,rowid,content,section,symbol)
                      VALUES ('delete',old.id,old.content,old.section,old.symbol);
                    END;
                    CREATE TRIGGER IF NOT EXISTS rag_au AFTER UPDATE ON rag_chunks BEGIN
                      INSERT INTO rag_fts(rag_fts,rowid,content,section,symbol)
                      VALUES ('delete',old.id,old.content,old.section,old.symbol);
                      INSERT INTO rag_fts(rowid,content,section,symbol)
                      VALUES (new.id,new.content,new.section,new.symbol);
                    END;
                    """
                )
                conn.execute("INSERT INTO rag_fts(rag_fts) VALUES('rebuild')")
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False

    @staticmethod
    def _safe_vec_name(signature: str, dimension: int) -> str:
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
        return f"rag_vec_{int(dimension)}_{digest}"

    def _open_vec(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.store.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self.sqlite_vec_available = True
            try:
                self.sqlite_vec_version = str(conn.execute("select vec_version()").fetchone()[0])
            except Exception:
                self.sqlite_vec_version = "available"
            return conn
        except Exception:
            conn.close()
            self.sqlite_vec_available = False
            raise

    def ensure_vector_index(self, provider_signature: str, dimension: int) -> VectorIndex:
        name = self._safe_vec_name(provider_signature, dimension)
        row = self.store.query_one("SELECT * FROM rag_vector_indexes WHERE index_name=?", (name,))
        if row:
            existing = VectorIndex(name, int(row["dimension"]), str(row["backend"]), str(row["provider_signature"]))
            # Provider/model migrations retain older indexes. If a previously
            # built index becomes current again, reactivate it deterministically
            # before returning it so status/search/reindex all target the same
            # logical embedding space.
            self.store.execute("UPDATE rag_vector_indexes SET active=CASE WHEN index_name=? THEN 1 ELSE 0 END", (name,))
            # A common upgrade path is: start WorkBot before installing the RAG
            # extra (Python fallback index), then install sqlite-vec later.
            # Promote the same logical index in-place and copy fallback vectors
            # so the operator does not have to delete/rebuild the corpus.
            if self.preferred_backend != "python" and existing.backend == "python":
                try:
                    conn = self._open_vec()
                    try:
                        conn.execute(
                            f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING vec0(partition_key text partition key, embedding float[{int(dimension)}] distance_metric=cosine)"
                        )
                        rows = self.store.query_all(
                            "SELECT chunk_id,embedding FROM rag_vector_fallback WHERE index_name=?", (name,)
                        )
                        with conn:
                            for item in rows:
                                detail = self.store.query_one(
                                    "SELECT d.source_type,d.namespace FROM rag_chunks c JOIN rag_documents d ON d.id=c.document_id WHERE c.id=?",
                                    (int(item['chunk_id']),),
                                )
                                part = self.partition_key(str(detail['source_type']) if detail else '', str(detail['namespace']) if detail else '')
                                conn.execute(f"DELETE FROM {name} WHERE rowid=?", (int(item['chunk_id']),))
                                conn.execute(
                                    f"INSERT INTO {name}(rowid,partition_key,embedding) VALUES (?,?,?)",
                                    (int(item['chunk_id']), part, bytes(item['embedding'])),
                                )
                        self.store.execute(
                            "UPDATE rag_vector_indexes SET backend='sqlite-vec' WHERE index_name=?", (name,)
                        )
                        return VectorIndex(name, int(dimension), "sqlite-vec", provider_signature)
                    finally:
                        conn.close()
                except Exception:
                    pass
            return existing

        backend = "python"
        if self.preferred_backend != "python":
            try:
                conn = self._open_vec()
                try:
                    conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING vec0(partition_key text partition key, embedding float[{int(dimension)}] distance_metric=cosine)"
                    )
                    conn.commit()
                    backend = "sqlite-vec"
                finally:
                    conn.close()
            except Exception:
                if not self.allow_python_fallback:
                    backend = "unavailable"

        self.store.execute("UPDATE rag_vector_indexes SET active=0")
        self.store.execute(
            "INSERT OR REPLACE INTO rag_vector_indexes(index_name,provider_signature,dimension,distance_metric,backend,active) VALUES (?,?,?,?,?,1)",
            (name, provider_signature, int(dimension), "cosine", backend),
        )
        return VectorIndex(name, int(dimension), backend, provider_signature)

    def active_vector_index(self) -> VectorIndex | None:
        row = self.store.query_one("SELECT * FROM rag_vector_indexes WHERE active=1 ORDER BY created_at DESC LIMIT 1")
        if not row:
            return None
        return VectorIndex(str(row["index_name"]), int(row["dimension"]), str(row["backend"]), str(row["provider_signature"]))

    def upsert_document(self, *, source_type: str, source_key: str, namespace: str, title: str,
                        source_uri: str, content_hash: str, metadata: dict | None = None) -> tuple[int, bool]:
        row = self.store.query_one("SELECT id,content_hash FROM rag_documents WHERE source_key=?", (source_key,))
        changed = row is None or str(row["content_hash"] or "") != str(content_hash or "")
        meta = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        if row:
            self.store.execute(
                "UPDATE rag_documents SET source_type=?,namespace=?,title=?,source_uri=?,content_hash=?,metadata_json=?,updated_at=unixepoch() WHERE id=?",
                (source_type, namespace, title, source_uri, content_hash, meta, int(row["id"])),
            )
            return int(row["id"]), changed
        cur = self.store.execute(
            "INSERT INTO rag_documents(source_type,source_key,namespace,title,source_uri,content_hash,metadata_json) VALUES (?,?,?,?,?,?,?)",
            (source_type, source_key, namespace, title, source_uri, content_hash, meta),
        )
        return int(cur.lastrowid), True

    def replace_chunks(self, document_id: int, chunks: Sequence[dict]) -> int:
        existing = {int(r["ordinal"]): r for r in self.store.query_all(
            "SELECT id,ordinal,content_hash FROM rag_chunks WHERE document_id=?", (int(document_id),)
        )}
        kept: set[int] = set()
        changed = 0
        with self.store.transaction() as conn:
            for ordinal, item in enumerate(chunks):
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                digest = str(item.get("content_hash") or hashlib.sha256(content.encode("utf-8")).hexdigest())
                old = existing.get(ordinal)
                meta = json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True)
                values = (
                    content, digest, int(item.get("token_count") or max(1, len(content) // 4)),
                    str(item.get("section") or ""), str(item.get("symbol") or ""),
                    str(item.get("start_ref") or ""), str(item.get("end_ref") or ""), meta,
                )
                if old:
                    chunk_id = int(old["id"])
                    if str(old["content_hash"] or "") != digest:
                        conn.execute(
                            "UPDATE rag_chunks SET content=?,content_hash=?,token_count=?,section=?,symbol=?,start_ref=?,end_ref=?,metadata_json=?,updated_at=unixepoch() WHERE id=?",
                            (*values, chunk_id),
                        )
                        conn.execute("DELETE FROM rag_chunk_embeddings WHERE chunk_id=?", (chunk_id,))
                        conn.execute("DELETE FROM rag_vector_fallback WHERE chunk_id=?", (chunk_id,))
                        changed += 1
                    else:
                        conn.execute(
                            "UPDATE rag_chunks SET token_count=?,section=?,symbol=?,start_ref=?,end_ref=?,metadata_json=? WHERE id=?",
                            (values[2], values[3], values[4], values[5], values[6], values[7], chunk_id),
                        )
                else:
                    cur = conn.execute(
                        "INSERT INTO rag_chunks(document_id,ordinal,content,content_hash,token_count,section,symbol,start_ref,end_ref,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (int(document_id), ordinal, *values),
                    )
                    chunk_id = int(cur.lastrowid)
                    changed += 1
                kept.add(ordinal)
            stale = [r for o, r in existing.items() if o not in kept]
            for row in stale:
                conn.execute("DELETE FROM rag_chunks WHERE id=?", (int(row["id"]),))
                changed += 1
        return changed

    def delete_missing_documents(self, source_types: Sequence[str], source_keys: set[str], *, source_key_prefix: str = "") -> int:
        if not source_types:
            return 0
        placeholders = ",".join("?" for _ in source_types)
        sql = f"SELECT id,source_key FROM rag_documents WHERE source_type IN ({placeholders})"
        params: tuple = tuple(source_types)
        if source_key_prefix:
            sql += " AND source_key LIKE ?"
            params = (*params, str(source_key_prefix) + "%")
        rows = self.store.query_all(sql, params)
        stale = [int(r["id"]) for r in rows if str(r["source_key"]) not in source_keys]
        if not stale:
            return 0
        with self.store.transaction() as conn:
            for doc_id in stale:
                conn.execute("DELETE FROM rag_documents WHERE id=?", (doc_id,))
        return len(stale)

    def pending_chunks(self, index: VectorIndex, limit: int) -> list[sqlite3.Row]:
        return self.store.query_all(
            """
            SELECT c.id,c.content,c.content_hash,d.source_type,d.source_key,d.title,d.source_uri,d.namespace,c.section,c.symbol
            FROM rag_chunks c JOIN rag_documents d ON d.id=c.document_id
            LEFT JOIN rag_chunk_embeddings e ON e.chunk_id=c.id AND e.index_name=?
            WHERE e.chunk_id IS NULL OR e.content_hash<>c.content_hash
            ORDER BY CASE d.source_type
                WHEN 'memory' THEN 0
                WHEN 'manual' THEN 1
                WHEN 'product-manual' THEN 1
                WHEN 'product_manual' THEN 1
                WHEN 'wiki' THEN 2
                WHEN 'web' THEN 3
                WHEN 'w3' THEN 3
                WHEN 'code' THEN 4
                WHEN 'workspace' THEN 5
                ELSE 3 END,
                d.updated_at DESC,c.id
            LIMIT ?
            """,
            (index.index_name, int(limit)),
        )

    @staticmethod
    def partition_key(source_type: str, namespace: str) -> str:
        source_type = str(source_type or "")
        namespace = str(namespace or "")
        if source_type == "memory":
            return f"memory:{namespace or 'global'}"
        if source_type in {"workspace", "code"}:
            return source_type
        return source_type or "knowledge"

    def upsert_vectors(self, index: VectorIndex, items: Sequence[tuple[int, str, str, Sequence[float]]]) -> None:
        if not items:
            return
        for chunk_id, _content_hash, _partition_key, vector in items:
            if len(vector) != index.dimension:
                raise ValueError(
                    f"RAG vector dimension mismatch for chunk {chunk_id}: "
                    f"got {len(vector)}, expected {index.dimension}"
                )
        if index.backend == "sqlite-vec":
            conn = self._open_vec()
            try:
                with conn:
                    for chunk_id, content_hash, partition_key, vector in items:
                        conn.execute(f"DELETE FROM {index.index_name} WHERE rowid=?", (int(chunk_id),))
                        conn.execute(
                            f"INSERT INTO {index.index_name}(rowid,partition_key,embedding) VALUES (?,?,?)",
                            (int(chunk_id), partition_key, serialize_f32(vector)),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO rag_chunk_embeddings(chunk_id,index_name,content_hash,embedded_at) VALUES (?,?,?,unixepoch())",
                            (int(chunk_id), index.index_name, content_hash),
                        )
                return
            finally:
                conn.close()
        if index.backend == "unavailable":
            return
        with self.store.transaction() as conn:
            for chunk_id, content_hash, _partition_key, vector in items:
                conn.execute(
                    "INSERT OR REPLACE INTO rag_vector_fallback(chunk_id,index_name,embedding) VALUES (?,?,?)",
                    (int(chunk_id), index.index_name, serialize_f32(vector)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO rag_chunk_embeddings(chunk_id,index_name,content_hash,embedded_at) VALUES (?,?,?,unixepoch())",
                    (int(chunk_id), index.index_name, content_hash),
                )

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\-./\\:]{2,}", query or "", flags=re.UNICODE)[:16]
        return " OR ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)

    def lexical_search(self, query: str, limit: int) -> list[dict]:
        out: list[dict] = []
        seen: set[int] = set()
        if self.fts_available:
            fts = self._fts_query(query)
            if fts:
                try:
                    rows = self.store.query_all(
                        """
                        SELECT c.id AS chunk_id,c.document_id,c.content,c.section,c.symbol,c.start_ref,c.end_ref,
                               d.source_type,d.source_key,d.namespace,d.title,d.source_uri,d.metadata_json,
                               bm25(rag_fts) AS lexical_rank
                        FROM rag_fts JOIN rag_chunks c ON c.id=rag_fts.rowid
                        JOIN rag_documents d ON d.id=c.document_id
                        WHERE rag_fts MATCH ? ORDER BY lexical_rank LIMIT ?
                        """,
                        (fts, int(limit)),
                    )
                    for r in rows:
                        item = dict(r); seen.add(int(r["chunk_id"])); out.append(item)
                except sqlite3.OperationalError:
                    pass
        if len(out) < limit:
            q = f"%{query}%"
            rows = self.store.query_all(
                """
                SELECT c.id AS chunk_id,c.document_id,c.content,c.section,c.symbol,c.start_ref,c.end_ref,
                       d.source_type,d.source_key,d.namespace,d.title,d.source_uri,d.metadata_json,
                       0.0 AS lexical_rank
                FROM rag_chunks c JOIN rag_documents d ON d.id=c.document_id
                WHERE c.content LIKE ? OR c.section LIKE ? OR c.symbol LIKE ? OR d.title LIKE ? OR d.source_uri LIKE ?
                ORDER BY d.updated_at DESC LIMIT ?
                """,
                (q, q, q, q, q, int(limit * 2)),
            )
            for r in rows:
                cid = int(r["chunk_id"])
                if cid not in seen:
                    out.append(dict(r)); seen.add(cid)
                    if len(out) >= limit:
                        break
        return out[:limit]

    def _details_for_ids(self, ids: Sequence[int]) -> dict[int, dict]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.store.query_all(
            f"""
            SELECT c.id AS chunk_id,c.document_id,c.content,c.section,c.symbol,c.start_ref,c.end_ref,c.content_hash,
                   d.source_type,d.source_key,d.namespace,d.title,d.source_uri,d.metadata_json
            FROM rag_chunks c JOIN rag_documents d ON d.id=c.document_id
            WHERE c.id IN ({placeholders})
            """,
            tuple(int(x) for x in ids),
        )
        return {int(r["chunk_id"]): dict(r) for r in rows}

    def vector_search(self, index: VectorIndex, query_vector: Sequence[float], limit: int, *, partition_keys: Sequence[str] | None = None) -> list[dict]:
        if index.backend == "unavailable":
            return []
        ranked: list[tuple[int, float]] = []
        if index.backend == "sqlite-vec":
            try:
                conn = self._open_vec()
                try:
                    if partition_keys:
                        combined = []
                        for part in partition_keys:
                            rows = conn.execute(
                                f"SELECT rowid,distance FROM {index.index_name} WHERE embedding MATCH ? AND partition_key=? AND k=? ORDER BY distance",
                                (serialize_f32(query_vector), str(part), int(limit)),
                            ).fetchall()
                            combined.extend((int(r[0]), float(r[1])) for r in rows)
                        combined.sort(key=lambda x: x[1])
                        ranked = combined[: int(limit)]
                    else:
                        rows = conn.execute(
                            f"SELECT rowid,distance FROM {index.index_name} WHERE embedding MATCH ? AND k=? ORDER BY distance",
                            (serialize_f32(query_vector), int(limit)),
                        ).fetchall()
                        ranked = [(int(r[0]), float(r[1])) for r in rows]
                finally:
                    conn.close()
            except Exception:
                ranked = []
        else:
            rows = self.store.query_all(
                "SELECT chunk_id,embedding FROM rag_vector_fallback WHERE index_name=?",
                (index.index_name,),
            )
            # The dependency-free fallback must obey the same partition filter as
            # sqlite-vec. Otherwise a global top-k can be consumed by vectors from
            # another conversation and only filtered afterwards, reducing recall
            # and weakening the memory isolation invariant.
            allowed_ids: set[int] | None = None
            if partition_keys:
                wanted_parts = {str(x) for x in partition_keys}
                candidate_ids = [int(r["chunk_id"]) for r in rows]
                details = self._details_for_ids(candidate_ids)
                allowed_ids = {
                    cid for cid, detail in details.items()
                    if self.partition_key(
                        str(detail.get("source_type") or ""),
                        str(detail.get("namespace") or ""),
                    ) in wanted_parts
                }
            q = list(float(x) for x in query_vector)
            qn = math.sqrt(sum(x*x for x in q)) or 1.0
            for r in rows:
                cid = int(r["chunk_id"])
                if allowed_ids is not None and cid not in allowed_ids:
                    continue
                v = deserialize_f32(bytes(r["embedding"]), index.dimension)
                vn = math.sqrt(sum(x*x for x in v)) or 1.0
                sim = sum(a*b for a,b in zip(q,v)) / (qn*vn)
                ranked.append((int(r["chunk_id"]), 1.0 - sim))
            ranked.sort(key=lambda x: x[1])
            ranked = ranked[: int(limit)]

        details = self._details_for_ids([x[0] for x in ranked])
        out: list[dict] = []
        for cid, distance in ranked:
            item = details.get(cid)
            if not item:
                continue
            marker = self.store.query_one(
                "SELECT content_hash FROM rag_chunk_embeddings WHERE chunk_id=? AND index_name=?",
                (cid, index.index_name),
            )
            if not marker or str(marker["content_hash"] or "") != str(item.get("content_hash") or ""):
                continue
            item = dict(item)
            item["distance"] = float(distance)
            out.append(item)
        return out

    def reset_index(self, index: VectorIndex) -> None:
        self.store.execute("DELETE FROM rag_chunk_embeddings WHERE index_name=?", (index.index_name,))
        self.store.execute("DELETE FROM rag_vector_fallback WHERE index_name=?", (index.index_name,))
        if index.backend == "sqlite-vec":
            try:
                conn = self._open_vec()
                try:
                    conn.execute(f"DELETE FROM {index.index_name}")
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass

    def status(self) -> dict:
        docs = self.store.query_one("SELECT COUNT(*) AS n FROM rag_documents")
        chunks = self.store.query_one("SELECT COUNT(*) AS n FROM rag_chunks")
        index = self.active_vector_index()
        embedded = 0
        if index:
            row = self.store.query_one("SELECT COUNT(*) AS n FROM rag_chunk_embeddings WHERE index_name=?", (index.index_name,))
            embedded = int(row["n"] if row else 0)
        return {
            "documents": int(docs["n"] if docs else 0),
            "chunks": int(chunks["n"] if chunks else 0),
            "fts": bool(self.fts_available),
            "vector_index": index.index_name if index else "",
            "vector_backend": index.backend if index else "not-initialized",
            "dimension": index.dimension if index else 0,
            "embedded_chunks": embedded,
            "sqlite_vec_available": bool(self.sqlite_vec_available),
            "sqlite_vec_version": self.sqlite_vec_version,
        }
