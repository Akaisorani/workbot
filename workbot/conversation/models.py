from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class IncomingMessage:
    platform: str
    conversation_id: str
    conversation_kind: str
    external_conversation_id: str
    external_message_id: str
    sender_id: str
    content: str
    sent_at_ms: int
    display_name: str = ""
    from_self: bool = False
    is_at: bool = False
    transport: str = ""
    dedup_key: str = ""
    content_key: str = ""
    media_only: bool = False
    media_types: tuple[str, ...] = ()
    quote: dict[str, Any] | None = None
    image_paths: tuple[str, ...] = ()
    image_context: tuple[dict[str, Any], ...] = ()
    # V1.12.3 peer-Agent collaboration metadata. These fields are populated
    # only after the WorkBot collaboration protocol has been parsed and the
    # declared peer identity has been verified against the actual IM sender.
    agent_peer_id: str = ""
    agent_message_id: str = ""
    agent_message_type: str = ""
    agent_reply_to: str = ""
    agent_hop: int = 0


@dataclass(slots=True)
class PendingAction:
    action_type: str
    payload: dict[str, Any]
    prompt: str
