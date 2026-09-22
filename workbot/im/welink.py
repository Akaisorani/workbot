from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import logging
import os
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .base import IMAdapter
from workbot.conversation.models import IncomingMessage
from workbot.im.welink_format import render_welink_markdown
from workbot.tools.shared_gate import SharedDirectoryLock
from workbot.tools.welink_rate import SharedRateBudget

log = logging.getLogger(__name__)


class WeLinkTransientError(RuntimeError):
    """Temporary CLI/PC-helper transport failure; caller may retry later."""


class WeLinkSendAmbiguousError(WeLinkTransientError):
    """Send returned an error and delivery could not be conclusively verified."""


class WeLinkRateLimitError(WeLinkTransientError):
    """WeLink explicitly rejected an operation because a rate limit was hit."""


@dataclass(slots=True)
class WeLinkConversation:
    kind: str  # group | user
    external_id: str
    display_name: str = ""
    next_poll_at: float = 0.0
    last_activity_at: float = 0.0
    last_polled_at: float = 0.0
    recent_rank: int = 999999
    recent_seen_at: float = 0.0
    boost_until: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.external_id}"


class WeLinkAdapter(IMAdapter):
    """Polling adapter for WeLink CLI.

    Loop prevention distinguishes this WorkBot's own output from peer WorkBots.
    A generic ``[AGENT]`` prefix is therefore not sufficient to suppress a
    group message: the sender must be one of this instance's configured self
    accounts, or the text must match the short-lived outgoing fingerprint cache.
    This keeps self-echo protection while allowing multiple WorkBots to share a
    group and exchange explicitly addressed collaboration messages.
    """

    def __init__(self, cli: str, groups: list[dict], reply_prefix: str = "[AGENT]",
                 bootstrap_from_latest: bool = True, transient_backoff_seconds: float = 30.0,
                 transient_backoff_max_seconds: float = 300.0,
                 send_verify_delay_seconds: float = 1.5,
                 cli_trace: bool = False,
                 cli_timeout_seconds: float = 90.0,
                 discovery: dict | None = None,
                 self_accounts: list[str] | None = None,
                 shared_gate_path: str | None = None,
                 shared_rate_path: str | None = None,
                 media_search_roots: list[str] | None = None,
                 quote_enabled: bool = True,
                 quote_max_chars: int = 10000):
        self.cli = cli
        self.self_accounts = {str(x) for x in (self_accounts or []) if str(x)}
        self._quote_enabled = bool(quote_enabled)
        self._quote_max_chars = max(200, int(quote_max_chars))
        self._media_search_roots: list[str] = []
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            for account in self.self_accounts:
                receive_root = Path(appdata) / "WeLink_Desktop" / "appdata" / "IM" / account / "ReceiveFiles"
                self._media_search_roots.extend([str(receive_root / "ScreenShot"), str(receive_root)])
        self._media_search_roots.extend(str(x) for x in (media_search_roots or []) if str(x).strip())
        self.discovery = discovery or {}
        self.discovery_mode = str(self.discovery.get("mode", "static")).lower()
        self._discovery_interval = max(2.0, float(self.discovery.get("refresh_seconds", 3)))
        self._next_discovery_at = 0.0
        # Accept both the V1.2 documented *_interval_seconds names and the
        # original internal *_poll_seconds aliases.
        self._active_poll_seconds = max(2.0, float(self.discovery.get("active_interval_seconds", self.discovery.get("active_poll_seconds", 5))))
        self._warm_poll_seconds = max(self._active_poll_seconds, float(self.discovery.get("warm_interval_seconds", self.discovery.get("warm_poll_seconds", 30))))
        self._quiet_poll_seconds = max(self._warm_poll_seconds, float(self.discovery.get("quiet_interval_seconds", self.discovery.get("quiet_poll_seconds", 300))))
        self._active_window_seconds = max(30.0, float(self.discovery.get("active_window_seconds", 300)))
        self._warm_window_seconds = max(self._active_window_seconds, float(self.discovery.get("warm_window_seconds", 3600)))
        self._max_conversations_per_poll = max(1, int(self.discovery.get("max_conversations_per_poll", 6)))
        # V1.2.1: discovery may return many recent conversations. Do not burst
        # one history request per conversation. Pace history queries globally
        # and normally issue only one per WorkBot poll tick.
        self._max_history_queries_per_poll = max(1, int(self.discovery.get("max_history_queries_per_poll", 1)))
        # Q17 documents query-history-message as 20 calls/minute. Keep a safety
        # margin by default (18/minute), burst=1, and enforce both a sliding
        # 60-second window and minimum spacing. This is stronger than sleep(3):
        # scheduler jitter can otherwise squeeze 20 calls into <60 seconds.
        self._history_limit_per_minute = max(1, int(self.discovery.get("history_limit_per_minute", 20)))
        self._history_budget_per_minute = max(1, min(
            self._history_limit_per_minute,
            int(self.discovery.get("history_budget_per_minute", 18)),
        ))
        documented_spacing = 60.0 / float(self._history_budget_per_minute)
        configured_spacing = float(self.discovery.get("history_min_interval_seconds", 0) or 0)
        self._history_min_interval_seconds = max(0.5, documented_spacing, configured_spacing)
        self._history_window_seconds = 60.0
        self._history_query_times: deque[float] = deque()
        self._rate_limit_cooldown_seconds = max(5.0, float(self.discovery.get("rate_limit_cooldown_seconds", 30)))
        self._next_history_query_at = 0.0
        self._rate_limit_until = 0.0
        self._rate_limit_hits = 0
        self._priority_groups = {str(x) for x in self.discovery.get("priority_groups", []) if str(x)}
        self._recent_focus_count = max(1, int(self.discovery.get("recent_focus_count", 3)))
        self._reply_boost_seconds = max(10.0, float(self.discovery.get("reply_boost_seconds", 120)))
        self._history_count = max(20, min(200, int(self.discovery.get("history_count", 50))))
        self._discovery_count = max(10, min(100, int(self.discovery.get("recent_count", 20))))
        self._conversations: dict[str, WeLinkConversation] = {}
        for g in groups:
            c = WeLinkConversation("group", str(g["group_id"]), str(g.get("group_name", "")))
            self._conversations[c.key] = c
        self.reply_prefix = reply_prefix
        # V1.10.3 default prefix is [AGENT].  Continue suppressing the legacy
        # prefix during rolling upgrades/history reconciliation so old bot
        # echoes cannot re-enter the conversation as user turns.
        self._legacy_reply_prefixes = tuple(
            p for p in ("[自动回复]",) if p and p != self.reply_prefix
        )
        self.bootstrap_from_latest = bootstrap_from_latest
        self._last_seen: dict[str, int] = {}
        self._last_seen_time_ms: dict[str, int] = {}
        self._bootstrapped: set[str] = set()
        self._recent_outgoing: deque[tuple[str, str, float]] = deque(maxlen=256)
        self._outgoing_ttl_seconds = 300.0
        self._transient_backoff_base = max(1.0, float(transient_backoff_seconds))
        self._transient_backoff_max = max(self._transient_backoff_base, float(transient_backoff_max_seconds))
        self._poll_backoff_until = 0.0
        self._poll_backoff_current = self._transient_backoff_base
        self._send_verify_delay_seconds = max(0.0, float(send_verify_delay_seconds))
        # welink-cli talks to the logged-in WeLink PC/helper through a local
        # verification/WebSocket path.  Running multiple CLI processes at the
        # same time can make that handshake contend.  Serialize *all* CLI
        # invocations from this WorkBot process, including poll, send, delivery
        # verification and durable retry.
        self._operation_lock = asyncio.Lock()
        self._cli_process_lock = threading.Lock()
        self._shared_gate = SharedDirectoryLock(shared_gate_path, timeout=120, stale_after=180) if shared_gate_path else None
        self._shared_rate = SharedRateBudget(shared_rate_path) if shared_rate_path else None
        self._cli_trace = bool(cli_trace)
        self._cli_timeout_seconds = max(5.0, float(cli_timeout_seconds))
        self._cli_calls = 0
        self._cli_failures = 0
        self._cli_transient_failures = 0
        self._cli_inflight = False
        self._cli_last_op = ""
        self._cli_last_command = ""
        self._cli_last_rc: int | None = None
        self._cli_last_duration = 0.0
        self._cli_last_wait = 0.0
        self._cli_last_error = ""
        self._cli_last_finished_at = 0.0
        # V1.5.1: reconnect recovery is a bounded sweep, not a fixed-duration
        # polling mode. Each conversation discovered for the sweep is queried
        # at most once before realtime becomes quiet again.
        self._backfill_requested = False
        self._backfill_pending: set[str] = set()
        self._backfill_started_at = 0.0
        self._backfill_completed_at = 0.0

    def set_last_seen(self, external_id: str, msg_id: int, kind: str = "group", *, sent_at_ms: int = 0) -> None:
        key = f"{kind}:{external_id}"
        self._last_seen[key] = int(msg_id)
        if sent_at_ms:
            self._last_seen_time_ms[key] = max(self._last_seen_time_ms.get(key, 0), int(sent_at_ms))
        self._bootstrapped.add(key)

    def set_last_seen_time(self, external_id: str, sent_at_ms: int, kind: str = "group") -> None:
        key = f"{kind}:{external_id}"
        if sent_at_ms:
            self._last_seen_time_ms[key] = max(self._last_seen_time_ms.get(key, 0), int(sent_at_ms))
        self._bootstrapped.add(key)

    def note_ingested_message(self, msg: IncomingMessage) -> None:
        """Advance fallback cursors only after the message is durably stored."""
        if msg.platform != "welink":
            return
        kind = "group" if msg.conversation_kind == "group" else "user"
        key = f"{kind}:{msg.external_conversation_id}"
        raw_id = str(msg.external_message_id)
        if raw_id.isdigit():
            self._last_seen[key] = max(self._last_seen.get(key, 0), int(raw_id))
        if msg.sent_at_ms:
            self._last_seen_time_ms[key] = max(self._last_seen_time_ms.get(key, 0), int(msg.sent_at_ms))
        self._bootstrapped.add(key)

    @staticmethod
    def _normalize_content(content: str) -> str:
        text = html.unescape(str(content or "")).strip()
        # Some API layers return a JSON string/object around the actual text.
        # Peel common wrappers without depending on one exact WeLink schema.
        for _ in range(2):
            if not text or text[0] not in "{[\"":
                break
            try:
                obj = json.loads(text)
            except Exception:
                break
            if isinstance(obj, str):
                text = obj.strip()
                continue
            if isinstance(obj, dict):
                for key in ("text", "content", "message", "msg"):
                    value = obj.get(key)
                    if isinstance(value, str):
                        text = value.strip()
                        break
                else:
                    break
                continue
            break
        return " ".join(text.split())

    @classmethod
    def _message_content_key(cls, sender: str, content: str) -> str:
        normalized = cls._normalize_content(content)
        if not normalized:
            return ""
        raw = f"{sender}\n{normalized}"
        return "welink-content:v1:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]

    @classmethod
    def _logical_message_key(cls, conversation_key: str, sender: str, content: str, sent_at_ms: int) -> str:
        """Transport-independent identity for Hook/history deduplication.

        WeLinkBot and ``query-history-message`` may expose different message IDs
        for the same logical message. ``serverSendTime`` plus conversation,
        sender and normalized text is stable across the two transports in the
        observed payloads. When no server timestamp exists, leave this blank and
        fall back to provider/local IDs rather than risk collapsing old messages.
        """
        if not sent_at_ms:
            return ""
        normalized = cls._normalize_content(content)
        if not normalized:
            return ""
        raw = f"{conversation_key}\n{sender}\n{int(sent_at_ms)}\n{normalized}"
        return "welink:v1:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]

    def _remember_outgoing(self, conversation_key: str, content: str) -> None:
        key = str(conversation_key)
        if ":" not in key:
            key = f"group:{key}"
        self._recent_outgoing.append((key, self._normalize_content(content), time.monotonic()))

    def _is_recent_outgoing(self, conversation_key: str, content: str) -> bool:
        now = time.monotonic()
        key = str(conversation_key)
        if ":" not in key:
            key = f"group:{key}"
        normalized = self._normalize_content(content)
        while self._recent_outgoing and now - self._recent_outgoing[0][2] > self._outgoing_ttl_seconds:
            self._recent_outgoing.popleft()
        return any(gid == key and sent == normalized for gid, sent, _ in self._recent_outgoing)

    def _is_bot_reply(self, conversation_key: str, content: str, *, sender_id: str = "", from_self: bool = False) -> bool:
        normalized = self._normalize_content(content)
        # The fingerprint is the strongest self-echo signal and remains useful
        # when Hook/history wrappers alter where the prefix appears.
        if self._is_recent_outgoing(conversation_key, content):
            return True
        # Legacy adapters without self_accounts cannot distinguish peers, so
        # preserve the historical prefix-only suppression in that configuration.
        # Multi-Agent collaboration therefore requires self_accounts to be set.
        sender_is_self = bool(
            from_self or (sender_id and sender_id in self.self_accounts) or not self.self_accounts
        )
        # V1.12.3 multi-Agent groups: a peer WorkBot may legitimately emit the
        # same [AGENT] prefix. Suppress prefix-marked content only when it came
        # from this WorkBot's own WeLink account.
        if sender_is_self:
            if self.reply_prefix and self.reply_prefix in normalized[:160]:
                return True
            if any(prefix in normalized[:160] for prefix in self._legacy_reply_prefixes):
                return True
            # V0.1.1 failure-loop signature: protect existing installations even
            # if a WeLink wrapper stripped/moved the prefix.
            if "CodeAgent 调用失败：" in normalized and "你仍可使用 /test" in normalized:
                return True
        return False

    @staticmethod
    def _strip_markup(value: str) -> str:
        """Best-effort text extraction for HTML/card fragments without regex-only XML parsing."""
        text = html.unescape(str(value or "")).strip()
        if not text:
            return ""
        # XML/HTML fragments that are well formed can be handled by ElementTree.
        for wrapped in (text, f"<root>{text}</root>"):
            try:
                root = ET.fromstring(wrapped)
                plain = " ".join(x.strip() for x in root.itertext() if x and x.strip())
                if plain:
                    return html.unescape(plain).strip()
            except Exception:
                pass
        # Final fallback is intentionally conservative and only strips tags from
        # a fragment already selected as a textual field.
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())

    @classmethod
    def _text_from_json_card(cls, obj: object, *, depth: int = 0) -> str:
        if depth > 4:
            return ""
        if isinstance(obj, str):
            visible = cls._strip_markup(obj)
            visible, _ = cls._strip_media_embeds(visible)
            return visible
        if isinstance(obj, dict):
            pieces: list[str] = []
            # Prefer explicit user-visible text fields and ignore IDs/URLs.
            for key in ("text", "content", "title", "summary", "description", "subtitle", "body"):
                if key in obj:
                    value = cls._text_from_json_card(obj.get(key), depth=depth + 1)
                    if value and value not in pieces:
                        pieces.append(value)
            if pieces:
                return " ".join(pieces)[:10000]
            for value in obj.values():
                text = cls._text_from_json_card(value, depth=depth + 1)
                if text:
                    return text[:10000]
        if isinstance(obj, list):
            pieces = [cls._text_from_json_card(x, depth=depth + 1) for x in obj[:20]]
            return " ".join(x for x in pieces if x)[:10000]
        return ""

    # WeLink UM media placeholders have been observed in at least two forms:
    #   /:um_begin{|Img|...}/:um_end
    #   /:um_begin{https://...|Img|...}/:um_end
    # Treat the brace payload as protocol metadata and identify a known media
    # kind from its leading pipe-delimited fields instead of assuming the
    # first field is always empty.
    _UM_MEDIA_RE = re.compile(
        r"/:um_begin\{(?P<payload>.*?)\}/:um_end",
        re.IGNORECASE | re.DOTALL,
    )
    _MEDIA_KIND_NAMES = {
        "img": "image", "image": "image", "pic": "image", "picture": "image",
        "file": "file", "attachment": "file",
        "audio": "audio", "voice": "audio",
        "video": "video",
    }

    @classmethod
    def _strip_media_embeds(cls, value: str) -> tuple[str, tuple[str, ...]]:
        """Remove WeLink UM media placeholders from user-visible text.

        WeLinkBot can expose an image/file as a textual placeholder such as
        ``/:um_begin{|Img|...}/:um_end`` or ``/:um_begin{URL|Img|...}/:um_end``.  That marker is protocol metadata,
        not a user question.  Return the remaining human text and media kinds.
        Unknown UM embed kinds are left untouched so we don't silently hide a
        future rich-text construct that may carry meaningful text.
        """
        text = str(value or "")
        kinds: list[str] = []

        def repl(match: re.Match[str]) -> str:
            payload = str(match.group("payload") or "")
            fields = [part.strip().lower() for part in payload.split("|")]
            # Current WeLink variants place the kind in field 0 or 1 (an
            # optional URL/empty field may precede it). Search only the first
            # few fields so arbitrary metadata later in the payload cannot
            # accidentally classify an unknown rich-text embed as media.
            kind = None
            for raw_kind in fields[:3]:
                kind = cls._MEDIA_KIND_NAMES.get(raw_kind)
                if kind:
                    break
            if not kind:
                return match.group(0)
            kinds.append(kind)
            return " "

        cleaned = cls._UM_MEDIA_RE.sub(repl, text)
        cleaned = " ".join(cleaned.split())
        return cleaned, tuple(dict.fromkeys(kinds))

    @classmethod
    def _media_types_from_data(cls, data: dict, raw_text: str = "") -> tuple[str, ...]:
        kinds: list[str] = []
        _, marker_kinds = cls._strip_media_embeds(raw_text)
        kinds.extend(marker_kinds)
        file_list = data.get("fileList")
        if isinstance(file_list, list) and file_list:
            for item in file_list:
                if not isinstance(item, dict):
                    kinds.append("file")
                    continue
                probe = " ".join(str(item.get(k, "")) for k in ("type", "file_type", "fileType", "mimeType", "name", "file_name")).lower()
                if any(x in probe for x in ("image", ".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    kinds.append("image")
                elif any(x in probe for x in ("audio", "voice", ".mp3", ".wav", ".m4a")):
                    kinds.append("audio")
                elif any(x in probe for x in ("video", ".mp4", ".mov", ".avi")):
                    kinds.append("video")
                else:
                    kinds.append("file")
        return tuple(dict.fromkeys(kinds))

    @staticmethod
    def _image_filename_from_um_payload(payload: str) -> str:
        fields = [str(x).strip() for x in str(payload or "").split("|")]
        kind_index = -1
        for i, field in enumerate(fields[:4]):
            if field.lower() in {"img", "image", "pic", "picture"}:
                kind_index = i
                break
        if kind_index < 0:
            return ""
        for field in fields[kind_index + 1: kind_index + 6]:
            name = os.path.basename(field.replace("\\", "/"))
            if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
                return name
        return ""

    def _candidate_image_path(self, filename: str) -> str:
        name = os.path.basename(str(filename or "").replace("\\", "/"))
        if not name:
            return ""
        for raw_root in self._media_search_roots:
            root = Path(os.path.expandvars(os.path.expanduser(raw_root)))
            candidate = root / name
            if candidate.exists():
                return str(candidate)
        # Preserve the most likely WeLink screenshot location even when the
        # desktop client has not finished writing the file yet. The enrichment
        # layer waits briefly before giving up.
        if self._media_search_roots:
            return str(Path(os.path.expandvars(os.path.expanduser(self._media_search_roots[0]))) / name)
        return ""

    def _image_paths_from_data(self, data: dict, raw_text: str = "") -> tuple[str, ...]:
        candidates: list[str] = []
        file_list = data.get("fileList")
        if isinstance(file_list, list):
            for item in file_list:
                if not isinstance(item, dict):
                    continue
                probe = " ".join(str(item.get(k, "")) for k in ("file_type", "fileType", "mimeType", "file_name", "name")).lower()
                if not any(x in probe for x in ("img", "image", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")):
                    continue
                for key in ("file_real_save_path", "file_path", "path", "localPath"):
                    value = str(item.get(key, "") or "").strip()
                    if value:
                        candidates.append(value)
                        break
                else:
                    filename = str(item.get("file_real_save_name") or item.get("file_name") or item.get("name") or "").strip()
                    candidate = self._candidate_image_path(filename)
                    if candidate:
                        candidates.append(candidate)

        show_html = html.unescape(str(data.get("showHtml", "") or ""))
        for pattern in (r'data-path=["\']([^"\']+)["\']', r'<img[^>]+src=["\']file:///([^"\']+)["\']'):
            for match in re.finditer(pattern, show_html, re.IGNORECASE):
                value = html.unescape(match.group(1)).strip()
                if value and "UC.ExternalImg" not in value:
                    candidates.append(value)

        for match in self._UM_MEDIA_RE.finditer(str(raw_text or "")):
            filename = self._image_filename_from_um_payload(match.group("payload"))
            candidate = self._candidate_image_path(filename)
            if candidate:
                candidates.append(candidate)

        # Preserve order and cap pathological payloads.
        return tuple(dict.fromkeys(str(x) for x in candidates if str(x)))[:8]


    @classmethod
    def _json_card_from_data(cls, data: dict) -> dict | None:
        """Best-effort extraction of a WeLink JSON card payload.

        Desktop Hook traffic often exposes ``showText`` + top-level ``quote``.
        Mobile quote/reply cards (cardType=65, chatContentType=10) instead keep
        the complete ``cardContext`` JSON inside the escaped XML ``content``
        envelope. Normalize both shapes before alias gating so the current
        reply text and quoted source have identical WorkBot semantics.
        """
        raw = str(data.get("content", "") or "").strip()
        if not raw:
            return None

        def load_candidate(value: str) -> dict | None:
            value = html.unescape(str(value or "")).strip()
            if not value or value[:1] not in "{[":
                return None
            try:
                obj = json.loads(value)
            except Exception:
                return None
            return obj if isinstance(obj, dict) else None

        for candidate in (raw, html.unescape(raw)):
            obj = load_candidate(candidate)
            if obj is not None:
                return obj

        try:
            outer = ET.fromstring(raw)
            encoded_inner = outer.findtext("c") or ""
            inner = html.unescape(encoded_inner).strip()
            if not inner:
                return None
            try:
                root = ET.fromstring(inner)
            except Exception:
                root = ET.fromstring(f"<root>{inner}</root>")
            for node in root.findall(".//content"):
                value = "".join(node.itertext()).strip()
                obj = load_candidate(value)
                if obj is not None:
                    return obj
        except Exception:
            return None
        return None

    @classmethod
    def _reply_text_from_card(cls, card: dict | None) -> str:
        if not isinstance(card, dict):
            return ""
        ctx = card.get("cardContext")
        if not isinstance(ctx, dict):
            return ""
        reply = ctx.get("replyMsg")
        if not isinstance(reply, dict):
            return ""
        return cls._quote_text(reply)

    @classmethod
    def _quote_text(cls, quote: dict) -> str:
        for key in ("showText", "content", "solidContent", "text"):
            value = quote.get(key)
            if isinstance(value, str) and value.strip():
                visible = cls._strip_markup(value)
                visible, _ = cls._strip_media_embeds(visible)
                if visible:
                    return visible[:10000]
        return ""

    def _quote_from_data(self, data: dict) -> dict | None:
        if not self._quote_enabled:
            return None
        raw = data.get("quote")
        if not isinstance(raw, dict):
            card = self._json_card_from_data(data)
            ctx = card.get("cardContext") if isinstance(card, dict) else None
            raw = ctx.get("preMsg") if isinstance(ctx, dict) else None
        if not isinstance(raw, dict):
            return None
        content = self._quote_text(raw)[: self._quote_max_chars]
        image_paths = self._image_paths_from_data(raw, str(raw.get("content", "") or ""))
        sender_name = str(raw.get("nameZH") or raw.get("nameEN") or raw.get("senderName") or "").strip()
        result = {
            "message_id": str(raw.get("messageID") or raw.get("msgId") or raw.get("id") or "").strip(),
            "sender_id": str(raw.get("sender") or raw.get("userAccount") or "").strip(),
            "sender_name": sender_name,
            "content": content,
            "type": raw.get("type"),
            "chat_content_type": raw.get("chatContentType"),
            "image_paths": list(image_paths),
        }
        if not any((result["message_id"], result["sender_id"], result["content"], image_paths)):
            return None
        return result

    @classmethod
    def _text_from_welinkbot_data(cls, data: dict) -> str:
        """Extract plain text from WeLinkBot Hook payloads.

        Hook traffic isn't one stable shape: DMs often expose ``showText``;
        classic IM uses escaped XML/CDATA; richer cards may carry JSON-ish
        content. Keep parsing layered and never feed the entire raw envelope to
        the Agent just because one representation changed.
        """
        for key in ("showText", "solidContent"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                visible = cls._strip_markup(value)[:10000]
                visible, _ = cls._strip_media_embeds(visible)
                if visible:
                    return visible
        # Mobile quote/reply events often have no showText. Their current
        # human reply lives in cardContext.replyMsg.content inside the JSON card.
        # Extract it before generic JSON/XML rendering; otherwise the whole card
        # becomes the message text and strict alias matching sees a leading ``{``.
        card = cls._json_card_from_data(data)
        reply_text = cls._reply_text_from_card(card)
        if reply_text:
            return reply_text[:10000]

        raw = str(data.get("content", "") or "").strip()
        if not raw:
            return ""
        raw_media_types = cls._media_types_from_data(data, raw)

        # Some card payloads are JSON directly (or HTML-escaped JSON).
        for candidate in (raw, html.unescape(raw)):
            if candidate[:1] in "{[":
                try:
                    text = cls._text_from_json_card(json.loads(candidate))
                    if text:
                        return text[:10000]
                except Exception:
                    pass

        try:
            outer = ET.fromstring(raw)
            encoded_inner = outer.findtext("c") or ""
            inner = html.unescape(encoded_inner).strip()
            if inner:
                try:
                    root = ET.fromstring(inner)
                except Exception:
                    root = ET.fromstring(f"<root>{inner}</root>")
                for tag in ("content", "text", "title", "summary", "html"):
                    for node in root.findall(f".//{tag}"):
                        value = "".join(node.itertext()).strip()
                        value = cls._strip_markup(value)
                        value, _ = cls._strip_media_embeds(value)
                        if value:
                            return value[:10000]
        except Exception:
            pass

        # If the event contains a known media embed and none of the structured
        # textual fields above produced human text, it is media-only.  Do not
        # fall back to the outer protocol envelope (which can contain UM marker
        # syntax and XML bookkeeping that looks like text to an LLM).
        if raw_media_types:
            return ""

        # Last resort: return a compact textual rendering, not the full XML/card
        # protocol payload. Non-text events with no useful representation are
        # intentionally ignored by returning an empty string.
        compact = cls._strip_markup(raw)
        compact, _ = cls._strip_media_embeds(compact)
        if compact and len(compact) <= 10000 and not compact.startswith("{\""):
            return compact[:10000]
        return ""

    @staticmethod
    def _push_message_id(data: dict, conversation_key: str, text: str) -> str:
        for key, prefix in (("msgId", ""), ("clientMsgId", "client:"), ("id", "local:")):
            value = data.get(key)
            if value not in (None, ""):
                return prefix + str(value)
        digest = hashlib.sha256(
            (conversation_key + "\n" + str(data.get("userAccount", "")) + "\n" +
             str(data.get("serverSendTime", data.get("timestamp", ""))) + "\n" + text).encode(
                 "utf-8", errors="replace"
             )
        ).hexdigest()[:24]
        return "hash:" + digest

    def normalize_push_event(self, event: dict) -> IncomingMessage | None:
        """Normalize one WeLinkBot WebSocket event into WorkBot's IM model.

        Important quirks observed in real Hook traffic:
        - ``isPrivateChat`` isn't reliable for DMs; use chatType/groupId.
        - a manual outgoing DM has ``isMine=true`` and should be stored as SELF.
        - group commands typed by the owner must *not* be treated as SELF merely
          because ``userAccount`` equals the logged-in account.
        """
        if not isinstance(event, dict):
            return None
        if event.get("type") != "weLinkMessage" or event.get("func") != "receiveIMMessage":
            return None
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        raw_content = ""
        for key in ("showText", "solidContent", "content"):
            value = str(data.get(key, "") or "")
            if value.strip():
                raw_content = value
                break
        media_types = self._media_types_from_data(data, raw_content)
        image_paths = self._image_paths_from_data(data, raw_content)
        quote = self._quote_from_data(data)
        text = self._text_from_welinkbot_data(data)
        media_only = bool(media_types and not text.strip())
        if not text and not media_only:
            return None
        if media_only:
            labels = {"image": "图片", "file": "文件", "audio": "语音", "video": "视频"}
            text = "[" + "/".join(labels.get(k, k) for k in media_types) + "]"

        group_id = str(data.get("groupId", "") or "").strip()
        try:
            chat_type = int(data.get("chatType", 1 if group_id else 0))
        except Exception:
            chat_type = 1 if group_id else 0
        is_group = bool(group_id and group_id not in {"0", "None"} and chat_type == 1)
        sender = str(data.get("userAccount", "") or "").strip()
        sender_name = str(data.get("senderNativeName") or data.get("senderName") or "").strip()
        sent_at = data.get("serverSendTime", data.get("timestamp", 0))
        try:
            sent_at_ms = int(sent_at or 0)
        except Exception:
            sent_at_ms = 0

        if is_group:
            conv = self._conversations.get(f"group:{group_id}")
            if conv is None:
                conv = WeLinkConversation("group", group_id, str(data.get("groupName", "") or group_id))
                self._conversations[conv.key] = conv
                log.info("Discovered WeLink group conversation from realtime push %s (%s)", conv.display_name, group_id)
            elif data.get("groupName"):
                conv.display_name = str(data.get("groupName"))
            # WorkBot's own CLI/app echo comes back through the Hook. Suppress
            # it before Conversation storage; owner's manually typed group
            # commands remain normal inbound messages.
            if self._is_bot_reply(conv.key, text, sender_id=sender, from_self=bool(data.get("isMine", False))):
                log.debug("Ignoring WorkBot realtime echo group=%s msgId=%s", group_id, data.get("msgId"))
                return None
            conversation_id = f"welink:group:{group_id}"
            external_id = group_id
            display_name = conv.display_name
            from_self = False
        else:
            is_mine = bool(data.get("isMine", False))
            # V1.11.1: Hook payloads for a manual local DM can expose
            # userAccount inconsistently. isMine is authoritative; normalize
            # the operator identity when exactly one self account is configured.
            if is_mine and len(self.self_accounts) == 1:
                sender = next(iter(self.self_accounts))
            receiver = str(data.get("receiverAccount", "") or "").strip()
            recent_owner = str(data.get("recentOwner", "") or "").strip()
            if is_mine:
                peer = receiver or recent_owner
            else:
                peer = sender or recent_owner or receiver
            if not peer:
                return None
            conv = self._conversations.get(f"user:{peer}")
            if conv is None:
                display = sender_name if not is_mine else peer
                conv = WeLinkConversation("user", peer, display or peer)
                self._conversations[conv.key] = conv
                log.info("Discovered WeLink user conversation from realtime push %s (%s)", conv.display_name, peer)
            elif not is_mine and sender_name:
                # A self-sent DM can discover the conversation before we know
                # the peer's human-readable name. Upgrade it on the first inbound
                # Hook message instead of permanently displaying the account id.
                conv.display_name = sender_name
            conversation_id = f"welink:user:{peer}"
            external_id = peer
            display_name = conv.display_name
            from_self = is_mine
            if from_self and self._is_bot_reply(conv.key, text, sender_id=sender, from_self=True):
                return None

        conv.last_activity_at = time.monotonic()
        conv.recent_seen_at = time.monotonic()
        conv.recent_rank = 0
        key = conv.key
        message_id = self._push_message_id(data, key, text)
        return IncomingMessage(
            platform="welink",
            conversation_id=conversation_id,
            conversation_kind="group" if is_group else "user",
            external_conversation_id=external_id,
            external_message_id=message_id,
            sender_id=sender,
            content=text,
            sent_at_ms=sent_at_ms,
            display_name=display_name,
            from_self=from_self,
            is_at=bool(data.get("isAt", False)),
            transport="welinkbot",
            dedup_key=self._logical_message_key(key, sender, text, sent_at_ms),
            content_key=self._message_content_key(sender, text),
            media_only=media_only,
            media_types=media_types,
            quote=quote,
            image_paths=image_paths,
        )

    def request_backfill(self) -> None:
        """Start one bounded history sweep after realtime connect/reconnect.

        The next poll performs one fresh recent-conversation discovery, freezes
        that result as the sweep target set, then queries each target at most
        once. This avoids keeping history polling hot for an arbitrary time
        window while WebSocket push is already healthy.
        """
        now = time.monotonic()
        self._backfill_requested = True
        self._backfill_pending.clear()
        self._next_discovery_at = 0.0
        for conv in self._conversations.values():
            conv.next_poll_at = min(conv.next_poll_at, now)

    @property
    def backfill_active(self) -> bool:
        return bool(self._backfill_requested or self._backfill_pending)

    @property
    def backfill_pending_count(self) -> int:
        return len(self._backfill_pending) + (1 if self._backfill_requested else 0)

    def cancel_backfill(self) -> None:
        self._backfill_requested = False
        self._backfill_pending.clear()

    @staticmethod
    def _is_rate_limit_error(text: str) -> bool:
        value = (text or "").lower()
        return "status 429" in value or "http 429" in value or "too many requests" in value or "rate limit" in value

    @staticmethod
    def _is_transient_poll_error(text: str) -> bool:
        value = (text or "").lower()
        needles = (
            "verification timeout",
            "welink pc verification timeout",
            "websocket",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "status 429",
            "http 429",
            "too many requests",
            "rate limit",
        )
        return any(x in value for x in needles)

    def _enter_poll_backoff(self, reason: str) -> None:
        now = time.monotonic()
        delay = self._poll_backoff_current
        self._poll_backoff_until = max(self._poll_backoff_until, now + delay)
        self._poll_backoff_current = min(self._transient_backoff_max, max(self._transient_backoff_base, delay * 2))
        log.warning("WeLink polling temporarily unavailable (%s); backing off %.0fs", reason, delay)

    def _reset_poll_backoff(self) -> None:
        self._poll_backoff_until = 0.0
        self._poll_backoff_current = self._transient_backoff_base

    def _enter_rate_limit_cooldown(self, reason: str) -> None:
        now = time.monotonic()
        self._rate_limit_hits += 1
        self._rate_limit_until = max(self._rate_limit_until, now + self._rate_limit_cooldown_seconds)
        self._next_history_query_at = max(self._next_history_query_at, self._rate_limit_until)
        log.warning(
            "WeLink rate limit detected; pausing history polling for %.0fs (%s)",
            self._rate_limit_cooldown_seconds, reason,
        )

    def _prune_history_budget(self, now: float) -> None:
        cutoff = now - self._history_window_seconds
        while self._history_query_times and self._history_query_times[0] <= cutoff:
            self._history_query_times.popleft()

    def _history_slot_available(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._prune_history_budget(now)
        if now < self._rate_limit_until or now < self._next_history_query_at:
            return False
        return len(self._history_query_times) < self._history_budget_per_minute

    def _consume_history_slot(self) -> None:
        now = time.monotonic()
        self._prune_history_budget(now)
        self._history_query_times.append(now)
        self._next_history_query_at = now + self._history_min_interval_seconds

    def _try_shared_history_reserve(self) -> tuple[bool, float]:
        if self._shared_rate is None:
            return True, 0.0
        ok, wait, _ = self._shared_rate.try_reserve(
            "im-history", limit=self._history_budget_per_minute,
            window_seconds=self._history_window_seconds,
            min_interval_seconds=self._history_min_interval_seconds,
        )
        if not ok:
            self._next_history_query_at = max(self._next_history_query_at, time.monotonic() + wait)
        return ok, wait

    def _history_budget_reset_in(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._prune_history_budget(now)
        if len(self._history_query_times) < self._history_budget_per_minute:
            return 0.0
        return max(0.0, self._history_query_times[0] + self._history_window_seconds - now)

    @staticmethod
    def _operation_name(args: tuple[str, ...] | list[str]) -> str:
        values = list(args)
        if len(values) >= 2:
            return f"{values[0]}.{values[1]}"
        return ".".join(values[:2]) or "unknown"

    def _display_command(self, args: tuple[str, ...] | list[str]) -> str:
        """Return a useful but payload-safe command line for diagnostics."""
        values = [str(x) for x in args]
        shown: list[str] = [str(self.cli)]
        i = 0
        while i < len(values):
            value = values[i]
            shown.append(value)
            if value == "--text" and i + 1 < len(values):
                payload = values[i + 1]
                digest = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:10]
                shown.append(f"<text len={len(payload)} sha256={digest}>")
                i += 2
                continue
            i += 1
        return subprocess.list2cmdline(shown)

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        op = self._operation_name(args)
        display = self._display_command(args)
        wait_started = time.monotonic()
        with self._cli_process_lock:
            local_waited = time.monotonic() - wait_started
            shared_waited = 0.0
            if self._shared_gate is not None:
                shared_waited = self._shared_gate.acquire()
            waited = local_waited + shared_waited
            started = time.monotonic()
            self._cli_inflight = True
            self._cli_last_op = op
            self._cli_last_command = display
            self._cli_last_wait = waited
            self._cli_calls += 1
            if self._cli_trace:
                log.info("WeLink CLI start op=%s wait=%.3fs cmd=%s", op, waited, display)
            try:
                cp = subprocess.run(
                    [self.cli, *args], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=self._cli_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                duration = time.monotonic() - started
                self._cli_failures += 1
                self._cli_transient_failures += 1
                self._cli_last_rc = None
                self._cli_last_duration = duration
                self._cli_last_error = f"subprocess timeout after {self._cli_timeout_seconds:.0f}s"
                self._cli_last_finished_at = time.time()
                log.warning("WeLink CLI timeout op=%s duration=%.3fs cmd=%s", op, duration, display)
                raise RuntimeError(
                    f"welink-cli timed out after {self._cli_timeout_seconds:.0f}s: {op}"
                ) from exc
            finally:
                self._cli_inflight = False
                if self._shared_gate is not None:
                    self._shared_gate.release()

            duration = time.monotonic() - started
            self._cli_last_rc = int(cp.returncode)
            self._cli_last_duration = duration
            self._cli_last_finished_at = time.time()
            detail = (cp.stderr.strip() or cp.stdout.strip())
            self._cli_last_error = "" if cp.returncode == 0 else detail[-1000:]
            if cp.returncode != 0:
                self._cli_failures += 1
                if self._is_transient_poll_error(detail):
                    self._cli_transient_failures += 1
                log.warning(
                    "WeLink CLI failed op=%s rc=%s wait=%.3fs duration=%.3fs cmd=%s error=%s",
                    op, cp.returncode, waited, duration, display, detail.splitlines()[-1][:300] if detail else "",
                )
            elif self._cli_trace:
                log.info("WeLink CLI done op=%s rc=0 wait=%.3fs duration=%.3fs", op, waited, duration)
            return cp

    def diagnostics(self) -> dict:
        remaining = max(0.0, self._poll_backoff_until - time.monotonic())
        return {
            "cli": self.cli,
            "cli_trace": self._cli_trace,
            "inflight": self._cli_inflight,
            "calls": self._cli_calls,
            "failures": self._cli_failures,
            "transient_failures": self._cli_transient_failures,
            "last_op": self._cli_last_op,
            "last_command": self._cli_last_command,
            "last_rc": self._cli_last_rc,
            "last_wait_seconds": round(self._cli_last_wait, 3),
            "last_duration_seconds": round(self._cli_last_duration, 3),
            "last_error": self._cli_last_error[-300:],
            "poll_backoff_remaining_seconds": round(remaining, 1),
            "history_pacing_remaining_seconds": round(max(0.0, self._next_history_query_at - time.monotonic()), 1),
            "rate_limit_remaining_seconds": round(max(0.0, self._rate_limit_until - time.monotonic()), 1),
            "rate_limit_hits": self._rate_limit_hits,
            "history_min_interval_seconds": round(self._history_min_interval_seconds, 3),
            "history_limit_per_minute": self._history_limit_per_minute,
            "history_budget_per_minute": self._history_budget_per_minute,
            "history_window_used": len(self._history_query_times),
            "history_budget_reset_seconds": round(self._history_budget_reset_in(), 1),
            "discovery_mode": self.discovery_mode,
            "discovery_interval_seconds": round(self._discovery_interval, 1),
            "discovery_next_seconds": round(max(0.0, self._next_discovery_at - time.monotonic()), 1),
            "recent_focus_count": self._recent_focus_count,
            "reply_boost_seconds": round(self._reply_boost_seconds, 1),
            "discovered_conversations": len(self._conversations),
            "discovery_count": self._discovery_count,
            "backfill_active": self.backfill_active,
            "backfill_pending": len(self._backfill_pending),
        }

    def _run_json(self, *args: str) -> dict:
        cp = self._run_cli(*args)
        if cp.returncode != 0:
            detail = cp.stderr.strip() or cp.stdout.strip()
            raise RuntimeError(f"welink-cli failed ({cp.returncode}): {detail}")
        try:
            return json.loads(cp.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"welink-cli returned non-JSON output: {cp.stdout[:500]!r}") from exc

    def _history_args(self, conv: WeLinkConversation, *, count: int | None = None) -> tuple[str, ...]:
        n = str(count or self._history_count)
        if conv.kind == "group":
            return ("im", "query-history-message", "--group-id", conv.external_id, "--query-count", n)
        return ("im", "query-history-message", "--user-account", conv.external_id, "--query-count", n)

    def _history_contains(self, conv: WeLinkConversation | str, full_text: str, *, created_at_ms: int) -> bool:
        if isinstance(conv, str):
            conv = WeLinkConversation("group", conv, conv)
        if not self._history_slot_available():
            raise WeLinkRateLimitError("history query pacing/cooldown active")
        try:
            obj = self._run_json(*self._history_args(conv, count=20))
        except RuntimeError as exc:
            if self._is_rate_limit_error(str(exc)):
                self._enter_rate_limit_cooldown(str(exc).splitlines()[-1][:240])
                raise WeLinkRateLimitError(str(exc)) from exc
            raise
        self._consume_history_slot()
        target = self._normalize_content(full_text)
        cutoff = int(created_at_ms) - 10_000
        chat = (((obj or {}).get("respData") or {}).get("chatInfo") or [])
        for item in chat:
            if item.get("contentType") != "TEXT_MSG":
                continue
            if int(item.get("serverSendTime", 0)) < cutoff:
                continue
            if self._normalize_content(str(item.get("content", ""))) == target:
                return True
        return False

    def _send_group_once(self, group_id: str, text: str) -> None:
        cp = self._run_cli("im", "send-to-group", "--group-id", group_id, "--text", text)
        if cp.returncode != 0:
            detail = cp.stderr.strip() or cp.stdout.strip()
            if self._is_rate_limit_error(detail):
                self._enter_rate_limit_cooldown(detail.splitlines()[-1][:240])
                raise WeLinkRateLimitError(detail)
            if self._is_transient_poll_error(detail):
                raise WeLinkTransientError(detail)
            raise RuntimeError(f"welink-cli send failed ({cp.returncode}): {detail}")

    def _send_user_once(self, account: str, text: str) -> None:
        cp = self._run_cli("im", "send-to-user", "--receiver", account, "--text", text)
        if cp.returncode != 0:
            detail = cp.stderr.strip() or cp.stdout.strip()
            if self._is_rate_limit_error(detail):
                self._enter_rate_limit_cooldown(detail.splitlines()[-1][:240])
                raise WeLinkRateLimitError(detail)
            if self._is_transient_poll_error(detail):
                raise WeLinkTransientError(detail)
            raise RuntimeError(f"welink-cli send failed ({cp.returncode}): {detail}")

    def _send_once(self, conv: WeLinkConversation, text: str) -> None:
        if conv.kind == "group":
            return self._send_group_once(conv.external_id, text)
        else:
            return self._send_user_once(conv.external_id, text)
    @staticmethod
    def _first_value(obj: dict, *keys: str) -> str:
        for key in keys:
            value = obj.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def _discover_conversations(self) -> set[str]:
        if self.discovery_mode != "all":
            return set(self._conversations)
        now = time.monotonic()
        if now < self._next_discovery_at:
            return set()
        obj = self._run_json("im", "query-recent-conversation", "--count", str(self._discovery_count))
        values = (obj or {}).get("conversation_info")
        if values is None:
            values = (((obj or {}).get("respData") or {}).get("conversationInfo") or
                      ((obj or {}).get("respData") or {}).get("conversation_info") or [])
        # Clear prior ranking; the current recent-conversation result is the
        # authoritative activity hint for this discovery round.
        for existing in self._conversations.values():
            existing.recent_rank = 999999

        recent_keys: set[str] = set()
        for rank, item in enumerate(values or []):
            if not isinstance(item, dict):
                continue
            gid = self._first_value(item, "group_id", "groupId")
            if gid and gid not in {"0", "None"}:
                name = self._first_value(item, "group_name", "groupName", "display_name", "displayName")
                conv = WeLinkConversation("group", gid, name)
            else:
                account = self._first_value(
                    item, "user_account", "userAccount", "peer_account", "peerAccount",
                    "receiver", "account", "user_id", "userId",
                )
                if not account:
                    continue
                name = self._first_value(item, "user_name", "userName", "display_name", "displayName", "name")
                conv = WeLinkConversation("user", account, name or account)
            old = self._conversations.get(conv.key)
            if old:
                if conv.display_name:
                    old.display_name = conv.display_name
                target = old
            else:
                self._conversations[conv.key] = conv
                target = conv
                log.info("Discovered WeLink %s conversation %s (%s)", conv.kind, conv.display_name or conv.external_id, conv.external_id)
            recent_keys.add(target.key)
            target.recent_rank = rank
            target.recent_seen_at = now
            # query-recent-conversation is our cheap activity signal. Pull the
            # top few conversations forward once their active polling interval
            # has elapsed, so a fresh /approve or direct message is not stuck
            # behind many overdue quiet rooms.
            if rank < self._recent_focus_count and now - target.last_polled_at >= self._active_poll_seconds:
                target.next_poll_at = min(target.next_poll_at, now)
        self._next_discovery_at = now + self._discovery_interval
        return recent_keys

    def _conversation_priority(self, conv: WeLinkConversation, now: float) -> tuple:
        recent = (
            conv.recent_rank < self._recent_focus_count
            and conv.recent_seen_at
            and now - conv.recent_seen_at <= max(10.0, self._discovery_interval * 3)
        )
        boosted = now < conv.boost_until
        privileged = conv.kind == "user" or conv.external_id in self._priority_groups
        active = bool(conv.last_activity_at and now - conv.last_activity_at <= self._active_window_seconds)
        if boosted:
            tier = 0
        elif privileged and recent:
            tier = 1
        elif recent:
            tier = 2
        elif privileged:
            tier = 3
        elif active:
            tier = 4
        else:
            tier = 5
        # Within the same tier, least-recently-polled first prevents one hot
        # room from permanently starving other recent/private conversations.
        return (tier, conv.last_polled_at, conv.next_poll_at, conv.recent_rank)

    def _poll_interval_for(self, conv: WeLinkConversation, now: float) -> float:
        if not conv.last_activity_at:
            return self._warm_poll_seconds
        age = now - conv.last_activity_at
        if age <= self._active_window_seconds:
            return self._active_poll_seconds
        if age <= self._warm_window_seconds:
            return self._warm_poll_seconds
        return self._quiet_poll_seconds

    def _conversation_from_id(self, conversation_id: str) -> WeLinkConversation:
        if conversation_id.startswith("welink:group:"):
            kind, external_id = "group", conversation_id.split(":", 2)[2]
        elif conversation_id.startswith("welink:user:"):
            kind, external_id = "user", conversation_id.split(":", 2)[2]
        else:
            raise ValueError(f"unsupported WeLink conversation: {conversation_id}")
        key = f"{kind}:{external_id}"
        conv = self._conversations.get(key)
        if conv is None:
            conv = WeLinkConversation(kind, external_id, external_id)
            self._conversations[key] = conv
        return conv

    async def poll(self) -> list[IncomingMessage]:
        # Serialize an entire poll operation with send/verify/retry operations.
        # This is intentionally stronger than merely locking subprocess.run: a
        # failed send followed by history verification should not be interleaved
        # with a background poll from another task.
        async with self._operation_lock:
            return await asyncio.to_thread(self._poll_sync)

    def _poll_sync(self) -> list[IncomingMessage]:
        now = time.monotonic()
        if now < self._poll_backoff_until:
            return []
        recent_keys: set[str] = set()
        try:
            # During a reconnect backfill sweep, discover once and then freeze
            # the target set. Re-running query-recent-conversation every 3s
            # while the sweep drains is unnecessary load.
            if self._backfill_requested or not self._backfill_pending:
                recent_keys = self._discover_conversations()
        except RuntimeError as exc:
            if self._is_rate_limit_error(str(exc)):
                self._enter_rate_limit_cooldown(str(exc).splitlines()[-1][:240])
                return []
            if self._is_transient_poll_error(str(exc)):
                self._enter_poll_backoff(str(exc).splitlines()[-1][:240])
                return []
            log.exception("WeLink conversation discovery failed")

        if self._backfill_requested:
            # In discovery=all mode, query only the conversations returned by
            # this fresh recent-conversation snapshot. In static mode there is
            # no discovery list, so reconcile the configured conversations.
            targets = recent_keys or set(self._conversations)
            self._backfill_pending = set(targets)
            self._backfill_requested = False
            self._backfill_started_at = now
            for key in self._backfill_pending:
                conv = self._conversations.get(key)
                if conv is not None:
                    conv.next_poll_at = min(conv.next_poll_at, now)
            log.info("WeLink bounded history backfill sweep started conversations=%d", len(self._backfill_pending))

        incoming: list[IncomingMessage] = []
        any_success = False
        # Never burst across all discovered conversations. A single recent-
        # conversation discovery result can fan out to N history API calls;
        # pacing that fan-out is essential because WeLink applies 429 limits.
        if not self._history_slot_available(now):
            return incoming
        candidates = (
            c for c in self._conversations.values()
            if c.next_poll_at <= now and (not self._backfill_pending or c.key in self._backfill_pending)
        )
        due = sorted(
            candidates,
            # A bounded backfill drains its frozen target set before any normal
            # periodic history work. Within the set, keep normal interaction
            # priority/fairness ordering.
            key=lambda c: self._conversation_priority(c, now),
        )[:min(self._max_conversations_per_poll, self._max_history_queries_per_poll)]
        for conv in due:
            shared_ok, shared_wait = self._try_shared_history_reserve()
            if not shared_ok:
                log.debug("WeLink shared history budget busy; next slot in %.2fs", shared_wait)
                break
            try:
                obj = self._run_json(*self._history_args(conv))
            except RuntimeError as exc:
                if self._is_rate_limit_error(str(exc)):
                    self._enter_rate_limit_cooldown(str(exc).splitlines()[-1][:240])
                    break
                if self._is_transient_poll_error(str(exc)):
                    self._enter_poll_backoff(str(exc).splitlines()[-1][:240])
                    break
                log.exception("WeLink conversation poll failed conversation=%s", conv.key)
                conv.next_poll_at = now + self._warm_poll_seconds
                continue

            self._consume_history_slot()
            conv.last_polled_at = now
            any_success = True
            if conv.key in self._backfill_pending:
                self._backfill_pending.discard(conv.key)
                if not self._backfill_pending:
                    self._backfill_completed_at = time.monotonic()
                    log.info("WeLink bounded history backfill sweep completed")
            chat = (((obj or {}).get("respData") or {}).get("chatInfo") or [])
            messages = sorted(chat, key=lambda x: (int(x.get("serverSendTime", 0)), int(x.get("msgId", 0))))
            max_id = max((int(x.get("msgId", 0)) for x in messages), default=0)
            key = conv.key
            if key not in self._bootstrapped:
                self._bootstrapped.add(key)
                if self.bootstrap_from_latest:
                    self._last_seen[key] = max_id
                    max_time = max((int(x.get("serverSendTime", 0)) for x in messages), default=0)
                    if max_time:
                        self._last_seen_time_ms[key] = max_time
                    conv.last_activity_at = now if messages else 0.0
                    conv.next_poll_at = now + self._poll_interval_for(conv, now)
                    log.info("Bootstrapped WeLink %s %s at msgId=%s", conv.kind, conv.display_name or conv.external_id, max_id)
                    continue

            last = self._last_seen.get(key, 0)
            last_time = self._last_seen_time_ms.get(key, 0)
            saw_new = False
            for item in messages:
                msg_id = int(item.get("msgId", 0))
                item_time = int(item.get("serverSendTime", 0))
                # IDs are the strongest cursor when available. serverSendTime is
                # the fallback needed for WeLinkBot DM pushes that expose only
                # clientMsgId/local id rather than the history API's msgId.
                if item.get("contentType") != "TEXT_MSG":
                    continue
                if last:
                    if msg_id <= last:
                        continue
                elif last_time and item_time <= last_time:
                    continue
                content = str(item.get("content", ""))
                visible_content, media_types = self._strip_media_embeds(content)
                image_paths = self._image_paths_from_data(item, content)
                quote = self._quote_from_data(item)
                media_only = bool(media_types and not visible_content)
                if media_only:
                    labels = {"image": "图片", "file": "文件", "audio": "语音", "video": "视频"}
                    visible_content = "[" + "/".join(labels.get(k, k) for k in media_types) + "]"
                elif visible_content:
                    content = visible_content
                if media_only:
                    content = visible_content
                sender = str(item.get("sender", ""))
                is_mine = bool(item.get("isMine", False))
                if conv.kind == "user" and is_mine and len(self.self_accounts) == 1:
                    sender = next(iter(self.self_accounts))
                if self._is_bot_reply(key, content, sender_id=sender, from_self=is_mine):
                    log.debug("Ignoring WorkBot self reply msgId=%s", msg_id)
                    continue
                # For a DM, query-history-message may include both directions.
                # Outgoing bot messages are already caught by prefix/fingerprint;
                # retain other entries as incoming because sender identifies peer.
                incoming.append(IncomingMessage(
                    platform="welink",
                    conversation_id=f"welink:{conv.kind}:{conv.external_id}",
                    conversation_kind="group" if conv.kind == "group" else "user",
                    external_conversation_id=conv.external_id,
                    external_message_id=str(msg_id),
                    sender_id=sender,
                    content=content,
                    sent_at_ms=int(item.get("serverSendTime", 0)),
                    display_name=conv.display_name,
                    from_self=bool(conv.kind == "user" and sender in self.self_accounts),
                    transport="welink-cli-history",
                    dedup_key=self._logical_message_key(key, sender, content, item_time),
                    content_key=self._message_content_key(sender, content),
                    media_only=media_only,
                    media_types=media_types,
                    quote=quote,
                    image_paths=image_paths,
                ))
                saw_new = True
            if max_id:
                self._last_seen[key] = max(last, max_id)
            max_time = max((int(x.get("serverSendTime", 0)) for x in messages), default=0)
            if max_time:
                self._last_seen_time_ms[key] = max(last_time, max_time)
            if saw_new:
                conv.last_activity_at = now
            conv.next_poll_at = now + self._poll_interval_for(conv, now)
            # A history slot has just been consumed; respect the global pacing
            # interval even if max_conversations_per_poll is configured > 1.
            if not self._history_slot_available():
                break

        if any_success:
            self._reset_poll_backoff()
        return incoming

    async def send_text(self, conversation_id: str, text: str, *, created_at_ms: int | None = None,
                        verify_before_send: bool = False) -> None:
        async with self._operation_lock:
            await self._send_text_locked(
                conversation_id, text, created_at_ms=created_at_ms,
                verify_before_send=verify_before_send,
            )

    async def _send_text_locked(self, conversation_id: str, text: str, *, created_at_ms: int | None = None,
                                verify_before_send: bool = False) -> None:
        conv = self._conversation_from_id(conversation_id)
        rendered = render_welink_markdown(text)
        full = rendered if rendered.startswith(self.reply_prefix) else f"{self.reply_prefix}{rendered}"
        created_at_ms = int(created_at_ms or time.time() * 1000)
        # For a durable retry, first check history so an earlier ambiguous send
        # is not duplicated after the CLI/PC helper reconnects.
        if verify_before_send:
            try:
                hist_target = conv.external_id if conv.kind == "group" else conv
                if await asyncio.to_thread(self._history_contains, hist_target, full, created_at_ms=created_at_ms):
                    self._remember_outgoing(conv.key, full)
                    return
            except RuntimeError as exc:
                if self._is_transient_poll_error(str(exc)):
                    raise WeLinkSendAmbiguousError(str(exc)) from exc
                raise
        self._remember_outgoing(conv.key, full)
        # After WorkBot asks a question/requests approval, keep the conversation
        # hot for a short period so control replies are observed promptly.
        conv.boost_until = max(conv.boost_until, time.monotonic() + self._reply_boost_seconds)
        conv.next_poll_at = min(conv.next_poll_at, time.monotonic())
        try:
            await asyncio.to_thread(self._send_once, conv, full)
            return
        except WeLinkRateLimitError as exc:
            # A 429 explicitly means the operation was rejected/rate limited.
            # Do not immediately issue another history request into the same
            # rate-limit window; leave the durable outbound row for retry.
            raise WeLinkSendAmbiguousError(str(exc)) from exc
        except WeLinkTransientError as exc:
            # Do not call `auth login` here.  WeLink normally refreshes tokens
            # itself while the PC client remains logged in.  A verification
            # timeout is treated as an ambiguous local transport failure.
            log.warning("WeLink send temporarily unavailable (%s); verifying delivery before retry", str(exc).splitlines()[-1][:240])
            if self._send_verify_delay_seconds:
                await asyncio.sleep(self._send_verify_delay_seconds)
            try:
                hist_target = conv.external_id if conv.kind == "group" else conv
                found = await asyncio.to_thread(self._history_contains, hist_target, full, created_at_ms=created_at_ms)
            except RuntimeError as verify_exc:
                raise WeLinkSendAmbiguousError(str(verify_exc)) from exc
            if found:
                log.info("WeLink send returned transient error but history confirms delivery")
                return
            raise WeLinkSendAmbiguousError(str(exc)) from exc

