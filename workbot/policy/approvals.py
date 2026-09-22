from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from workbot.storage.sqlite import Store
from workbot.transport.protocol import new_id

_TERMINAL = {"approved", "denied", "expired", "cancelled", "interrupted"}


@dataclass(slots=True)
class ApprovalResult:
    approval_id: str
    decision: str
    reason: str = ""
    decided_by: str | None = None
    changed: bool = False

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


class ApprovalManager:
    """Durable approval requests plus in-process waiters.

    Approval requests are durable for audit/history, but a synchronous Linux
    caller cannot survive a WorkBot process restart because its SSH RPC request
    disappears with the process.  Pending requests found during startup are
    therefore marked ``interrupted`` instead of being silently approved/replayed.
    """

    def __init__(self, store: Store, cfg: dict | None = None):
        self.store = store
        self.cfg = cfg or {}
        self.timeout_seconds = float(self.cfg.get("approval_timeout_seconds", 900))
        self.approver_senders = {str(x) for x in self.cfg.get("approver_senders", []) if str(x)}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self.store.execute(
            "UPDATE approval_requests SET state='interrupted', reason='WorkBot restarted while approval was pending', "
            "decided_at=unixepoch() WHERE state='pending'"
        )

    def create(self, *, conversation_id: str, node: str | None, task_id: str | None,
               action: str, summary: str, details: dict[str, Any] | None = None,
               requested_by: str | None = None, timeout_seconds: float | None = None) -> str:
        approval_id = new_id("approval")
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        expires_at = int(time.time() + max(1.0, timeout))
        self.store.execute(
            """
            INSERT INTO approval_requests(
                approval_id, conversation_id, node, task_id, action, summary,
                details_json, state, requested_by, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                approval_id, conversation_id, node, task_id, action, summary,
                json.dumps(details or {}, ensure_ascii=False), requested_by, expires_at,
            ),
        )
        return approval_id

    def get(self, approval_id: str):
        return self.store.query_one("SELECT * FROM approval_requests WHERE approval_id=?", (approval_id,))

    def pending(self, *, conversation_id: str | None = None, limit: int = 20) -> list:
        if conversation_id:
            return self.store.query_all(
                "SELECT * FROM approval_requests WHERE state='pending' AND conversation_id=? "
                "ORDER BY created_at DESC LIMIT ?", (conversation_id, limit),
            )
        return self.store.query_all(
            "SELECT * FROM approval_requests WHERE state='pending' ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def sender_can_decide(self, sender_id: str) -> bool:
        return not self.approver_senders or str(sender_id) in self.approver_senders

    def decide(self, approval_id: str, *, approved: bool, sender_id: str, reason: str = "") -> ApprovalResult:
        row = self.get(approval_id)
        if not row:
            raise KeyError(f"unknown approval {approval_id}")
        if row["state"] != "pending":
            return ApprovalResult(approval_id, row["state"], row["reason"] or "", row["decided_by"], False)
        if not self.sender_can_decide(sender_id):
            raise PermissionError(f"sender {sender_id} is not an approval operator")
        decision = "approved" if approved else "denied"
        self.store.execute(
            "UPDATE approval_requests SET state=?, reason=?, decided_by=?, decided_at=unixepoch() WHERE approval_id=?",
            (decision, reason, str(sender_id), approval_id),
        )
        result = ApprovalResult(approval_id, decision, reason, str(sender_id), True)
        for fut in self._waiters.pop(approval_id, []):
            if not fut.done():
                fut.set_result(result)
        return result

    def mark_resume_pending(self, approval_id: str) -> None:
        self.store.execute(
            "UPDATE approval_requests SET resume_state='pending' WHERE approval_id=?",
            (approval_id,),
        )

    def claim_resume(self, approval_id: str) -> bool:
        """Atomically claim the one allowed post-approval Agent continuation."""
        result = self.store.execute(
            "UPDATE approval_requests SET resume_state='running' "
            "WHERE approval_id=? AND state='approved' AND resume_state IN ('pending','none')",
            (approval_id,),
        )
        return result.rowcount == 1

    def finish_resume(self, approval_id: str, *, success: bool) -> None:
        self.store.execute(
            "UPDATE approval_requests SET resume_state=?, resumed_at=unixepoch() "
            "WHERE approval_id=? AND resume_state='running'",
            ("completed" if success else "failed", approval_id),
        )

    async def wait(self, approval_id: str, *, timeout: float | None = None) -> ApprovalResult:
        row = self.get(approval_id)
        if not row:
            raise KeyError(approval_id)
        if row["state"] in _TERMINAL:
            return ApprovalResult(approval_id, row["state"], row["reason"] or "", row["decided_by"], False)
        remaining = max(0.0, float(row["expires_at"] or 0) - time.time())
        if timeout is not None:
            remaining = min(remaining, max(0.0, float(timeout)))
        if remaining <= 0:
            self._expire(approval_id)
            return ApprovalResult(approval_id, "expired", "approval timed out")
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(approval_id, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=remaining)
        except asyncio.TimeoutError:
            self._expire(approval_id)
            return ApprovalResult(approval_id, "expired", "approval timed out")
        finally:
            waiters = self._waiters.get(approval_id)
            if waiters and fut in waiters:
                waiters.remove(fut)
                if not waiters:
                    self._waiters.pop(approval_id, None)

    def _expire(self, approval_id: str) -> None:
        self.store.execute(
            "UPDATE approval_requests SET state='expired', reason='approval timed out', decided_at=unixepoch() "
            "WHERE approval_id=? AND state='pending'", (approval_id,),
        )
        result = ApprovalResult(approval_id, "expired", "approval timed out")
        for fut in self._waiters.pop(approval_id, []):
            if not fut.done():
                fut.set_result(result)
