from __future__ import annotations

import json
import sqlite3
import hashlib
import html
from .models import IncomingMessage, PendingAction
from workbot.storage.sqlite import Store


class ConversationManager:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _context_payload(msg: IncomingMessage) -> dict:
        payload: dict = {}
        if msg.quote:
            payload["quote"] = msg.quote
        if msg.image_paths:
            payload["image_paths"] = list(msg.image_paths)
        if msg.image_context:
            payload["image_context"] = list(msg.image_context)
        if msg.agent_peer_id:
            payload["agent_peer"] = {
                "peer_id": msg.agent_peer_id,
                "message_id": msg.agent_message_id,
                "type": msg.agent_message_type,
                "reply_to": msg.agent_reply_to,
                "hop": int(msg.agent_hop or 0),
            }
        return payload

    @staticmethod
    def _decode_context(value: str | None) -> dict:
        if not value:
            return {}
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    @classmethod
    def format_context_for_agent(cls, context: dict | None) -> str:
        context = dict(context or {})
        lines: list[str] = []
        quote = context.get("quote")
        if isinstance(quote, dict):
            sender = str(quote.get("sender_name") or quote.get("sender_id") or "unknown")
            sender_id = str(quote.get("sender_id") or "").strip()
            suffix = f" ({sender_id})" if sender_id and sender_id != sender else ""
            message_id = str(quote.get("message_id") or "").strip()
            lines.append(f"[Quoted message from {sender}{suffix}" + (f", id={message_id}]" if message_id else "]"))
            content = str(quote.get("content") or "").strip()
            if content:
                lines.append(content)
            for item in quote.get("image_context") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("path") or "image")
                text = str(item.get("text") or "").strip()
                if item.get("status") in {"ok", "empty", "available"}:
                    lines.append(f"[Quoted image file for direct multimodal inspection: {name}]")
                if text:
                    lines.append(f"[Quoted image OCR supplement: {name}]\n{text}")
                elif item.get("status") == "available":
                    lines.append(f"[Quoted image OCR: {item.get('reason') or 'not run'}]")
                elif item.get("reason") and item.get("status") not in {"ok", "empty", "available"}:
                    lines.append(f"[Quoted image unavailable: {name}; {item.get('reason')}]")
        peer = context.get("agent_peer")
        if isinstance(peer, dict) and peer.get("peer_id"):
            bits = [f"peer={peer.get('peer_id')}"]
            if peer.get("message_id"):
                bits.append(f"message_id={peer.get('message_id')}")
            if peer.get("type"):
                bits.append(f"type={peer.get('type')}")
            if peer.get("reply_to"):
                bits.append(f"reply_to={peer.get('reply_to')}")
            bits.append(f"hop={int(peer.get('hop') or 0)}")
            lines.append("[Verified peer Agent message: " + "; ".join(bits) + "]")
        for item in context.get("image_context") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("path") or "image")
            text = str(item.get("text") or "").strip()
            if item.get("status") in {"ok", "empty", "available"}:
                lines.append(f"[Image file for direct multimodal inspection: {name}]")
            if text:
                lines.append(f"[Image OCR supplement: {name}]\n{text}")
            elif item.get("status") == "available":
                lines.append(f"[Image OCR: {item.get('reason') or 'not run'}]")
            elif item.get("reason") and item.get("status") not in {"ok", "empty", "available"}:
                lines.append(f"[Image unavailable: {name}; {item.get('reason')}]")
        return "\n".join(lines).strip()

    def format_message_for_agent(self, msg: IncomingMessage, *, content: str | None = None) -> str:
        text = str(msg.content if content is None else content).strip()
        rich = self.format_context_for_agent(self._context_payload(msg))
        if not rich:
            return text
        return f"{text}\n\n--- Trusted transport / quoted / image context ---\n{rich}".strip()

    def ensure(self, msg: IncomingMessage) -> None:
        self.store.execute(
            """
            INSERT INTO conversations(conversation_id, platform, kind, external_id, display_name, updated_at)
            VALUES (?, ?, ?, ?, ?, unixepoch())
            ON CONFLICT(conversation_id) DO UPDATE SET
              display_name=excluded.display_name,
              updated_at=unixepoch()
            """,
            (msg.conversation_id, msg.platform, msg.conversation_kind,
             msg.external_conversation_id, msg.display_name),
        )

    def add_incoming(self, msg: IncomingMessage) -> bool:
        """Durably ingest one logical message exactly once.

        WeLinkBot Hook push and the OAuth history API do not always expose the
        same identifier for the same message.  In particular, locally-sent
        messages may have only ``clientMsgId``/a local Hook id while history
        later supplies a numeric ``msgId``.  Deduplicate first by the provider
        ID, then by a transport-independent fingerprint.  A narrow timestamp
        fallback covers small Hook/API timestamp skew without collapsing two
        identical messages delivered by the same transport.
        """
        self.ensure(msg)
        direction = 'self' if msg.from_self else 'in'
        dedup_key = msg.dedup_key or None
        content_key = msg.content_key or None
        if content_key is None and msg.content:
            normalized = " ".join(html.unescape(str(msg.content)).split())
            if normalized:
                raw = f"{msg.sender_id}\n{normalized}"
                content_key = "welink-content:v1:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]
        with self.store.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? AND external_message_id=? LIMIT 1",
                (msg.conversation_id, msg.external_message_id),
            ).fetchone():
                return False
            if dedup_key and conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? AND dedup_key=? LIMIT 1",
                (msg.conversation_id, dedup_key),
            ).fetchone():
                return False

            # Legacy rows from <=1.5.1 have no dedup_key. Also tolerate a small
            # serverSendTime discrepancy between Hook and history, but only
            # across different transports (or legacy NULL transport) so two
            # intentional repeated messages on the same channel are preserved.
            if msg.sent_at_ms and msg.content:
                lo = int(msg.sent_at_ms) - 2500
                hi = int(msg.sent_at_ms) + 2500
                row = conn.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE conversation_id=? AND sender_id=?
                      AND (content_key=? OR (content_key IS NULL AND content=?))
                      AND sent_at BETWEEN ? AND ?
                      AND (transport IS NULL OR transport<>?)
                    LIMIT 1
                    """,
                    (msg.conversation_id, msg.sender_id, content_key, msg.content, lo, hi, msg.transport or ''),
                ).fetchone()
                if row:
                    return False

            try:
                conn.execute(
                    """
                    INSERT INTO messages(
                        conversation_id, external_message_id, sender_id, direction, content, sent_at, transport, dedup_key, content_key, context_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (msg.conversation_id, msg.external_message_id, msg.sender_id,
                     direction, msg.content, msg.sent_at_ms, msg.transport or None, dedup_key, content_key,
                     json.dumps(self._context_payload(msg), ensure_ascii=False) if self._context_payload(msg) else None),
                )
            except sqlite3.IntegrityError:
                # Another transport/task may have won the race on either unique
                # identity while this message was being normalized.
                return False
            conn.execute(
                "UPDATE conversations SET last_message_id=?, updated_at=unixepoch() WHERE conversation_id=?",
                (msg.external_message_id, msg.conversation_id),
            )
        return True

    def add_outgoing(self, conversation_id: str, content: str, external_message_id: str) -> None:
        self.store.execute(
            """
            INSERT OR IGNORE INTO messages(conversation_id, external_message_id, sender_id, direction, content, sent_at)
            VALUES (?, ?, 'workbot', 'out', ?, NULL)
            """,
            (conversation_id, external_message_id, content),
        )

    def get(self, conversation_id: str):
        return self.store.query_one("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,))

    def update_message_context(self, msg: IncomingMessage) -> None:
        payload = self._context_payload(msg)
        self.store.execute(
            "UPDATE messages SET context_json=? WHERE conversation_id=? AND external_message_id=?",
            (json.dumps(payload, ensure_ascii=False) if payload else None, msg.conversation_id, msg.external_message_id),
        )

    @classmethod
    def _rows_with_context(cls, rows) -> list[dict]:
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            item["context"] = cls._decode_context(item.pop("context_json", None))
            out.append(item)
        return out

    def recent_messages(self, conversation_id: str, limit: int = 12) -> list[dict]:
        rows = self.store.query_all(
            """
            SELECT id, sender_id, direction, content, sent_at, created_at, context_json
            FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?
            """,
            (conversation_id, limit),
        )
        return self._rows_with_context(reversed(rows))

    def recent_messages_from_sender(self, conversation_id: str, sender_id: str, *,
                                    before_external_message_id: str | None = None, limit: int = 12) -> list[dict]:
        """Return recent human/self messages from one sender before a reference message.

        Used by delegated execution (for example, an operator saying
        ``处理上面 p300... 的任务``).  The original message stays the durable
        provenance source while the current operator supplies authorization.
        """
        before_id = None
        if before_external_message_id:
            row = self.store.query_one(
                "SELECT id FROM messages WHERE conversation_id=? AND external_message_id=?",
                (conversation_id, before_external_message_id),
            )
            before_id = int(row["id"]) if row else None
        if before_id is None:
            rows = self.store.query_all(
                """
                SELECT id, external_message_id, sender_id, direction, content, sent_at, created_at, context_json
                FROM messages
                WHERE conversation_id=? AND lower(sender_id)=lower(?) AND direction IN ('in','self')
                ORDER BY id DESC LIMIT ?
                """,
                (conversation_id, sender_id, limit),
            )
        else:
            rows = self.store.query_all(
                """
                SELECT id, external_message_id, sender_id, direction, content, sent_at, created_at, context_json
                FROM messages
                WHERE conversation_id=? AND lower(sender_id)=lower(?) AND direction IN ('in','self') AND id<?
                ORDER BY id DESC LIMIT ?
                """,
                (conversation_id, sender_id, before_id, limit),
            )
        return self._rows_with_context(rows)

    def message_count(self, conversation_id: str) -> int:
        row = self.store.query_one("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conversation_id,))
        return int(row["n"] if row else 0)

    def transcript_since(self, conversation_id: str, after_count: int = 0, limit: int = 80) -> list[dict]:
        # Message IDs are monotonic and more useful than timestamps for local summarization.
        rows = self.store.query_all(
            """
            SELECT id, sender_id, direction, content, sent_at, created_at, context_json
            FROM messages WHERE conversation_id=? AND id>? ORDER BY id LIMIT ?
            """,
            (conversation_id, after_count, limit),
        )
        return self._rows_with_context(rows)

    def set_summary(self, conversation_id: str, summary: str, message_count: int) -> None:
        self.store.execute(
            "UPDATE conversations SET summary=?, summary_message_count=?, updated_at=unixepoch() WHERE conversation_id=?",
            (summary.strip(), int(message_count), conversation_id),
        )

    def set_pending_action(self, conversation_id: str, action: PendingAction | None) -> None:
        value = None if action is None else json.dumps(
            {"action_type": action.action_type, "payload": action.payload, "prompt": action.prompt},
            ensure_ascii=False,
        )
        self.store.execute(
            "UPDATE conversations SET pending_action_json=?, updated_at=unixepoch() WHERE conversation_id=?",
            (value, conversation_id),
        )

    def get_pending_action(self, conversation_id: str) -> PendingAction | None:
        row = self.get(conversation_id)
        if not row or not row["pending_action_json"]:
            return None
        obj = json.loads(row["pending_action_json"])
        return PendingAction(obj["action_type"], obj["payload"], obj["prompt"])

    def consume_pending_response(self, conversation_id: str, text: str, *,
                                 confirm_words: set[str], cancel_words: set[str]) -> tuple[str | None, PendingAction | None]:
        """Atomically consume a confirmation/cancellation for the current proposal.

        V1.1 keeps proposal replies out of the reasoning-agent path. Clearing the
        JSON in the same SQLite transaction that reads it also prevents duplicate
        IM delivery/concurrent control paths from executing one proposal twice.
        """
        value = text.strip()
        if value not in confirm_words and value not in cancel_words:
            return None, None
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT pending_action_json FROM conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not row or not row["pending_action_json"]:
                return None, None
            obj = json.loads(row["pending_action_json"])
            conn.execute(
                "UPDATE conversations SET pending_action_json=NULL, updated_at=unixepoch() WHERE conversation_id=?",
                (conversation_id,),
            )
        pending = PendingAction(obj["action_type"], obj["payload"], obj["prompt"])
        return ("confirm" if value in confirm_words else "cancel"), pending

    def set_agent_session(self, conversation_id: str, session_id: str | None) -> None:
        self.store.execute(
            """
            UPDATE conversations SET agent_session_id=?,
                session_turns=CASE WHEN ? IS NULL THEN 0 ELSE session_turns END,
                session_updated_at=CASE WHEN ? IS NULL THEN NULL ELSE unixepoch() END,
                updated_at=unixepoch()
            WHERE conversation_id=?
            """,
            (session_id, session_id, session_id, conversation_id),
        )

    def note_agent_turn(self, conversation_id: str, session_id: str | None) -> None:
        self.store.execute(
            """
            UPDATE conversations SET
              agent_session_id=COALESCE(?, agent_session_id),
              session_turns=session_turns+1,
              session_updated_at=unixepoch(), updated_at=unixepoch()
            WHERE conversation_id=?
            """,
            (session_id, conversation_id),
        )

    def clear_agent_session(self, conversation_id: str) -> None:
        self.store.execute(
            "UPDATE conversations SET agent_session_id=NULL, session_turns=0, session_updated_at=NULL, updated_at=unixepoch() "
            "WHERE conversation_id=?",
            (conversation_id,),
        )
