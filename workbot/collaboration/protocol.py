from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

_PROTOCOL_MARKER = "[WBOT]"
_AGENT_PREFIX_RE = re.compile(r"^\s*\[AGENT[^\]]*\]\s*", re.IGNORECASE)
_ALLOWED_TYPES = {"request", "response", "event"}
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class AgentEnvelope:
    version: int
    from_id: str
    to_id: str
    message_type: str
    message_id: str
    reply_to: str | None = None
    hop: int = 0


def new_agent_message_id() -> str:
    return "amsg-" + uuid.uuid4().hex[:16]

_DISCOVERY_KINDS = {"hello", "hello_ack"}


def encode_discovery_body(kind: str, *, agent_id: str, display_name: str = "", aliases=(), capabilities=()) -> str:
    """Encode a small, non-secret peer-discovery payload carried by an event envelope.

    The payload is descriptive only. Identity is still authenticated by the IM
    sender account observed by the receiving WorkBot.
    """
    kind = str(kind or "").strip().lower()
    if kind not in _DISCOVERY_KINDS:
        raise ValueError(f"unsupported discovery kind: {kind}")
    payload = {
        "kind": kind,
        "agent_id": str(agent_id or "").strip(),
        "display_name": str(display_name or "").strip(),
        "aliases": [str(x) for x in aliases if str(x).strip()][:16],
        "capabilities": [str(x) for x in capabilities if str(x).strip()][:32],
    }
    if not payload["agent_id"] or not _ID_RE.match(payload["agent_id"]):
        raise ValueError("invalid agent_id")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def decode_discovery_body(body: str) -> dict | None:
    try:
        raw = json.loads(str(body or "").strip())
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    agent_id = str(raw.get("agent_id") or "").strip()
    if kind not in _DISCOVERY_KINDS or not agent_id or not _ID_RE.match(agent_id):
        return None
    aliases = [str(x) for x in (raw.get("aliases") or []) if str(x).strip()][:16]
    capabilities = [str(x) for x in (raw.get("capabilities") or []) if str(x).strip()][:32]
    return {
        "kind": kind,
        "agent_id": agent_id,
        "display_name": str(raw.get("display_name") or "").strip(),
        "aliases": aliases,
        "capabilities": capabilities,
    }


def encode_agent_message(envelope: AgentEnvelope, body: str) -> str:
    meta = {
        "v": int(envelope.version),
        "from": str(envelope.from_id),
        "to": str(envelope.to_id),
        "type": str(envelope.message_type),
        "id": str(envelope.message_id),
        "hop": int(envelope.hop),
    }
    if envelope.reply_to:
        meta["reply_to"] = str(envelope.reply_to)
    header = json.dumps(meta, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{_PROTOCOL_MARKER}{header}\n{str(body or '').strip()}".rstrip()


def decode_agent_message(text: str) -> tuple[AgentEnvelope, str] | None:
    """Decode a WorkBot-to-WorkBot group message.

    The transport-level human-visible prefix may be shared (``[AGENT]``) or
    bot-specific (for example ``[AGENT-A]``). Identity does not come from that
    prefix; callers must verify ``from`` against the actual IM sender account.
    """
    value = str(text or "").strip()
    value = _AGENT_PREFIX_RE.sub("", value, count=1)
    if not value.startswith(_PROTOCOL_MARKER):
        return None
    rest = value[len(_PROTOCOL_MARKER):].lstrip()
    if not rest.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    try:
        raw, end = decoder.raw_decode(rest)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    body = rest[end:].lstrip(" \t\r\n")
    try:
        version = int(raw.get("v", 0))
        from_id = str(raw.get("from") or "").strip()
        to_id = str(raw.get("to") or "").strip()
        message_type = str(raw.get("type") or "").strip().lower()
        message_id = str(raw.get("id") or "").strip()
        reply_to = str(raw.get("reply_to") or "").strip() or None
        hop = int(raw.get("hop", 0))
    except Exception:
        return None
    if version != 1:
        return None
    if not from_id or not to_id or message_type not in _ALLOWED_TYPES or not message_id:
        return None
    if not _ID_RE.match(from_id) or not (_ID_RE.match(to_id) or to_id == "*") or not _ID_RE.match(message_id):
        return None
    if reply_to and not _ID_RE.match(reply_to):
        return None
    if hop < 0 or hop > 100:
        return None
    return AgentEnvelope(version, from_id, to_id, message_type, message_id, reply_to, hop), body
