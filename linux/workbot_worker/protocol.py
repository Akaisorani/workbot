from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class Message:
    type: str
    id: str = field(default_factory=lambda: new_id("msg"))
    version: int = PROTOCOL_VERSION
    ts: float = field(default_factory=time.time)
    method: str | None = None
    event: str | None = None
    task_id: str | None = None
    correlation_id: str | None = None
    source: str | None = None
    target: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {
            "version": self.version, "type": self.type, "id": self.id, "ts": self.ts,
            "method": self.method, "event": self.event, "task_id": self.task_id,
            "correlation_id": self.correlation_id, "source": self.source,
            "target": self.target, "data": self.data,
        }.items() if v is not None}

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def loads(cls, line: str) -> "Message":
        obj = json.loads(line)
        return cls(
            version=obj.get("version", 1), type=obj["type"], id=obj.get("id", new_id("msg")),
            ts=obj.get("ts", time.time()), method=obj.get("method"), event=obj.get("event"),
            task_id=obj.get("task_id"), correlation_id=obj.get("correlation_id"),
            source=obj.get("source"), target=obj.get("target"), data=obj.get("data") or {},
        )



def request(method: str, *, data: dict[str, Any] | None = None, task_id: str | None = None,
            source: str | None = None, target: str | None = None) -> Message:
    return Message(type="request", method=method, task_id=task_id, source=source, target=target, data=data or {})

def response(req: Message, data: dict[str, Any]) -> Message:
    return Message(type="response", correlation_id=req.id, task_id=req.task_id,
                   source=req.target, target=req.source, data=data)


def event(name: str, *, task_id: str | None = None, source: str | None = None,
          data: dict[str, Any] | None = None) -> Message:
    return Message(type="event", event=name, task_id=task_id, source=source, data=data or {})
