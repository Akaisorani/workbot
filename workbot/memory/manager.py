from __future__ import annotations

import re
from workbot.storage.sqlite import Store


class MemoryManager:
    def __init__(self, store: Store, cfg: dict | None = None):
        self.store = store
        self.cfg = cfg or {}
        self.cross_conversation_retrieval = bool(self.cfg.get("cross_conversation_retrieval", True))

    def remember(self, scope: str, title: str, content: str, tags: str = "", source: str | None = None,
                 *, memory_kind: str = "fact", confidence: float = 1.0,
                 evidence: str | None = None, auto_generated: bool = False) -> int:
        title = title.strip() or "WorkBot memory"
        content = content.strip()
        fp = self.store.memory_fingerprint(scope, title, content)
        existing = self.store.query_one("SELECT id FROM memory_items WHERE fingerprint=?", (fp,))
        promotion_state = "candidate" if auto_generated and scope.startswith("conversation:") else "none"
        if existing:
            self.store.execute(
                "UPDATE memory_items SET tags=?, source=COALESCE(?,source), confidence=max(confidence,?), "
                "evidence=COALESCE(?,evidence), auto_generated=max(auto_generated,?), "
                "promotion_state=CASE WHEN promotion_state='none' AND ?='candidate' THEN 'candidate' ELSE promotion_state END, "
                "updated_at=unixepoch() WHERE id=?",
                (tags, source, float(confidence), evidence, 1 if auto_generated else 0, promotion_state, existing["id"]),
            )
            return int(existing["id"])
        cur = self.store.execute(
            """
            INSERT INTO memory_items(scope,memory_kind,title,content,tags,source,fingerprint,confidence,evidence,auto_generated,promotion_state)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (scope, memory_kind, title, content, tags, source, fp, float(confidence), evidence, 1 if auto_generated else 0, promotion_state),
        )
        return int(cur.lastrowid)


    def upsert_source(self, scope: str, title: str, content: str, tags: str = "", source: str | None = None,
                      *, memory_kind: str = "fact", confidence: float = 1.0,
                      evidence: str | None = None, auto_generated: bool = False) -> int:
        """Upsert a durable memory by stable source identity.

        This is used for generated project/workspace knowledge whose content is
        expected to evolve.  Ordinary ``remember`` remains fingerprint-based so
        conversational facts are never silently overwritten by unrelated text.
        """
        if not source:
            return self.remember(scope, title, content, tags, source, memory_kind=memory_kind, confidence=confidence, evidence=evidence, auto_generated=auto_generated)
        existing = self.store.query_one("SELECT id FROM memory_items WHERE scope=? AND source=? ORDER BY id LIMIT 1", (scope, source))
        title = title.strip() or "WorkBot memory"
        content = content.strip()
        fp = self.store.memory_fingerprint(scope, title, content)
        if existing:
            self.store.execute(
                "UPDATE memory_items SET memory_kind=?,title=?,content=?,tags=?,fingerprint=?,confidence=?,evidence=?,auto_generated=?,updated_at=unixepoch() WHERE id=?",
                (memory_kind,title,content,tags,fp,float(confidence),evidence,1 if auto_generated else 0,existing["id"]),
            )
            return int(existing["id"])
        return self.remember(scope, title, content, tags, source, memory_kind=memory_kind, confidence=confidence, evidence=evidence, auto_generated=auto_generated)

    def forget(self, memory_id: int) -> bool:
        cur = self.store.execute("DELETE FROM memory_items WHERE id=?", (int(memory_id),))
        return cur.rowcount > 0

    def get(self, memory_id: int):
        return self.store.query_one("SELECT * FROM memory_items WHERE id=?", (int(memory_id),))

    def list(self, *, scopes: list[str] | None = None, limit: int = 20):
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            return self.store.query_all(
                f"SELECT * FROM memory_items WHERE scope IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                (*scopes, int(limit)),
            )
        return self.store.query_all("SELECT * FROM memory_items ORDER BY updated_at DESC LIMIT ?", (int(limit),))

    @staticmethod
    def _fts_query(query: str) -> str:
        # Keep this deliberately conservative; LIKE fallback handles Chinese
        # substrings and punctuation-heavy paths better than FTS alone.
        tokens = re.findall(r"[\w\-\.]{2,}", query, flags=re.UNICODE)[:12]
        return " OR ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)

    def search(self, query: str, limit: int = 5, *, scopes: list[str] | None = None):
        query = (query or "").strip()
        if not query:
            return self.list(scopes=scopes, limit=limit)
        rows: list = []
        seen: set[int] = set()
        if self.store.fts5_available:
            fts = self._fts_query(query)
            if fts:
                try:
                    if scopes:
                        placeholders = ",".join("?" for _ in scopes)
                        found = self.store.query_all(
                            f"""
                            SELECT m.*, bm25(memory_fts) AS rank
                            FROM memory_fts JOIN memory_items m ON m.id=memory_fts.rowid
                            WHERE memory_fts MATCH ? AND m.scope IN ({placeholders})
                            ORDER BY rank, m.updated_at DESC LIMIT ?
                            """,
                            (fts, *scopes, int(limit)),
                        )
                    else:
                        found = self.store.query_all(
                            """
                            SELECT m.*, bm25(memory_fts) AS rank
                            FROM memory_fts JOIN memory_items m ON m.id=memory_fts.rowid
                            WHERE memory_fts MATCH ? ORDER BY rank, m.updated_at DESC LIMIT ?
                            """,
                            (fts, int(limit)),
                        )
                    for r in found:
                        if int(r["id"]) not in seen:
                            rows.append(r); seen.add(int(r["id"]))
                except Exception:
                    pass

        # LIKE is both fallback and supplement: it is much better for exact
        # paths, identifiers, and Chinese substrings in typical SQLite builds.
        if len(rows) < limit:
            q = f"%{query}%"
            if scopes:
                placeholders = ",".join("?" for _ in scopes)
                found = self.store.query_all(
                    f"""
                    SELECT * FROM memory_items
                    WHERE scope IN ({placeholders}) AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (*scopes, q, q, q, int(limit * 2)),
                )
            else:
                found = self.store.query_all(
                    "SELECT * FROM memory_items WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (q, q, q, int(limit * 2)),
                )
            for r in found:
                if int(r["id"]) not in seen:
                    rows.append(r); seen.add(int(r["id"]))
                    if len(rows) >= limit:
                        break

        for r in rows:
            try:
                self.store.execute("UPDATE memory_items SET last_used_at=unixepoch() WHERE id=?", (r["id"],))
            except Exception:
                pass
        return rows[:limit]

    def forget_auto_summary(self) -> int:
        cur = self.store.execute("DELETE FROM memory_items WHERE auto_generated=1 AND source LIKE 'summary:%'")
        return int(cur.rowcount or 0)

    def forget_all_auto(self) -> int:
        cur = self.store.execute("DELETE FROM memory_items WHERE auto_generated=1")
        return int(cur.rowcount or 0)

    def promotion_candidates(self, limit: int = 20):
        return self.store.query_all(
            "SELECT * FROM memory_items WHERE scope LIKE 'conversation:%' AND auto_generated=1 "
            "AND promotion_state='candidate' AND (promotion_reviewed_at IS NULL OR promotion_reviewed_at < unixepoch()-604800) "
            "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (int(limit),),
        )

    def mark_promotion_reviewed(self, memory_ids: list[int]) -> None:
        if not memory_ids:
            return
        placeholders = ",".join("?" for _ in memory_ids)
        self.store.execute(
            f"UPDATE memory_items SET promotion_reviewed_at=unixepoch() WHERE id IN ({placeholders})",
            tuple(int(x) for x in memory_ids),
        )

    def mark_promoted(self, memory_ids: list[int], *, global_memory_id: int) -> None:
        if not memory_ids:
            return
        placeholders = ",".join("?" for _ in memory_ids)
        self.store.execute(
            f"UPDATE memory_items SET promotion_state='promoted',promotion_reviewed_at=unixepoch(),promoted_global_id=? WHERE id IN ({placeholders})",
            (int(global_memory_id), *tuple(int(x) for x in memory_ids)),
        )

    def relevant_scopes(self, conversation_id: str | None = None) -> list[str]:
        # V1.11.1: projects commonly span several WeLink groups/DMs.  An empty
        # scope list means "all scopes" to list/search, while the original scope
        # remains stored as provenance.  Operators can opt back into the strict
        # V1.11.0 behavior with cross_conversation_retrieval=false.
        if self.cross_conversation_retrieval:
            return []
        scopes = ["global"]
        if conversation_id:
            scopes.append(f"conversation:{conversation_id}")
        return scopes
