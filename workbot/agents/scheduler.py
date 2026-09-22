from __future__ import annotations

import asyncio
import heapq
import itertools
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class SchedulerStats:
    active: int
    stopping: int
    waiting: int
    max_concurrent: int


class AgentInvocationStopped(RuntimeError):
    """One CodeAgent invocation was explicitly stopped by the operator."""


@dataclass(slots=True)
class AgentInvocation:
    invocation_id: str
    state: str
    priority: int
    task: asyncio.Task | None
    task_name: str
    cancel_task: asyncio.Task | None = None
    waiter_future: asyncio.Future | None = None
    purpose: str = "agent"
    conversation_id: str = ""
    session_id: str = ""
    detail: str = ""
    created_at: float = field(default_factory=time.time)
    created_monotonic: float = field(default_factory=time.monotonic)
    started_at: float = 0.0
    started_monotonic: float = 0.0
    cancel_requested: bool = False

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {
            "invocation_id": self.invocation_id,
            "state": self.state,
            "priority": self.priority,
            "task_name": self.task_name,
            "purpose": self.purpose,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "detail": self.detail,
            "created_at": self.created_at,
            "wait_seconds": round(max(0.0, (self.started_monotonic or now) - self.created_monotonic), 3),
            "run_seconds": round(max(0.0, now - self.started_monotonic), 3) if self.started_monotonic else 0.0,
            "cancel_requested": self.cancel_requested,
        }


class AgentScheduler:
    """Priority gate plus runtime registry for scarce CodeAgent capacity.

    Lower numeric priority runs first. The registry is the source of truth for
    active/waiting counts; there is deliberately no independent ``_active``
    counter that can leak during a cancellation/handoff race.
    """

    def __init__(self, max_concurrent: int = 2):
        self.max_concurrent = max(1, int(max_concurrent))
        self._seq = itertools.count()
        self._waiters: list[tuple[int, int, str, asyncio.Future[None]]] = []
        self._invocations: dict[str, AgentInvocation] = {}
        self._lock = asyncio.Lock()

    def _active_count(self) -> int:
        return sum(1 for inv in self._invocations.values() if inv.state == "active")

    def _stopping_count(self) -> int:
        return sum(1 for inv in self._invocations.values() if inv.state == "stopping")

    def _occupied_count(self) -> int:
        # A stopping invocation no longer counts as user-visible ``active``,
        # but its subprocess is still being reaped. Do not hand its slot to a
        # new CodeAgent until cleanup finishes, otherwise max_concurrent can be
        # exceeded briefly during operator cancellation.
        return self._active_count() + self._stopping_count()

    def _new_invocation(self, priority: int, metadata: dict | None) -> AgentInvocation:
        meta = metadata or {}
        task = asyncio.current_task()
        inv = AgentInvocation(
            invocation_id="agent-" + uuid.uuid4().hex[:12],
            state="waiting",
            priority=int(priority),
            task=task,
            task_name=task.get_name() if task else "",
            purpose=str(meta.get("purpose") or "agent"),
            conversation_id=str(meta.get("conversation_id") or ""),
            session_id=str(meta.get("session_id") or ""),
            detail=str(meta.get("detail") or "")[:500],
        )
        self._invocations[inv.invocation_id] = inv
        return inv

    @staticmethod
    def _activate(inv: AgentInvocation) -> None:
        inv.state = "active"
        inv.started_at = time.time()
        inv.started_monotonic = time.monotonic()

    async def _acquire(self, inv: AgentInvocation) -> None:
        async with self._lock:
            if self._occupied_count() < self.max_concurrent and not self._waiters:
                self._activate(inv)
                return
            fut = asyncio.get_running_loop().create_future()
            inv.waiter_future = fut
            heapq.heappush(self._waiters, (inv.priority, next(self._seq), inv.invocation_id, fut))
        try:
            await fut
        except asyncio.CancelledError:
            # A cancellation can race with _release(): the waiter may already
            # have been promoted to active immediately before this task observes
            # cancellation. If so, release that handed-off slot explicitly.
            if inv.state == "active":
                await self._release(inv.invocation_id)
            else:
                self._invocations.pop(inv.invocation_id, None)
            current = asyncio.current_task()
            if inv.cancel_requested and (current is None or not current.cancelling()):
                raise AgentInvocationStopped(f"CodeAgent invocation {inv.invocation_id} stopped while waiting")
            raise

    async def _release(self, invocation_id: str) -> None:
        async with self._lock:
            self._invocations.pop(invocation_id, None)
            while self._waiters and self._occupied_count() < self.max_concurrent:
                _priority, _seq, next_id, fut = heapq.heappop(self._waiters)
                nxt = self._invocations.get(next_id)
                if fut.cancelled() or nxt is None or nxt.state != "waiting":
                    continue
                self._activate(nxt)
                fut.set_result(None)
                # One released slot wakes one waiter. Multiple free slots are
                # filled by independent releases/new arrivals.
                break

    @asynccontextmanager
    async def slot(self, priority: int = 10, *, metadata: dict | None = None):
        inv = self._new_invocation(priority, metadata)
        acquired = False
        try:
            await self._acquire(inv)
            acquired = True
            yield inv.invocation_id
        finally:
            if acquired:
                await self._release(inv.invocation_id)
            else:
                self._invocations.pop(inv.invocation_id, None)

    def stats(self) -> SchedulerStats:
        return SchedulerStats(
            self._active_count(),
            self._stopping_count(),
            sum(1 for inv in self._invocations.values() if inv.state == "waiting"),
            self.max_concurrent,
        )

    def invocations(self) -> list[dict]:
        rank = {"stopping": 0, "active": 1, "waiting": 2}
        rows = [x.snapshot() for x in self._invocations.values() if x.state in {"active", "stopping", "waiting"}]
        rows.sort(key=lambda x: (rank.get(x["state"], 9), -float(x.get("run_seconds") or 0), -float(x.get("wait_seconds") or 0)))
        return rows


    def invocations_for_conversation(self, conversation_id: str) -> list[dict]:
        cid = str(conversation_id or "")
        if not cid:
            return []
        return [
            inv.snapshot()
            for inv in self._invocations.values()
            if inv.conversation_id == cid and inv.state in {"active", "stopping", "waiting"}
        ]

    def has_live_for_conversation(self, conversation_id: str) -> bool:
        cid = str(conversation_id or "")
        return bool(cid) and any(
            inv.conversation_id == cid and inv.state in {"active", "stopping", "waiting"}
            for inv in self._invocations.values()
        )

    async def wait_until_gone(self, invocation_id: str, timeout: float = 2.0) -> bool:
        """Wait briefly for one invocation to leave the runtime registry.

        This never consumes a CodeAgent slot and is intended for the fast
        operator control plane. It gives /stop-session a truthful confirmation
        without waiting indefinitely for OS process cleanup.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        key = str(invocation_id)
        while key in self._invocations:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True

    def bind_cancel_task(self, invocation_id: str, task: asyncio.Task) -> None:
        inv = self._invocations.get(str(invocation_id))
        if inv is not None:
            inv.cancel_task = task

    def cancel(self, invocation_id: str) -> dict | None:
        inv = self._invocations.get(str(invocation_id))
        if not inv or inv.state not in {"active", "stopping", "waiting"}:
            return None

        # Repeated /stop-session must be idempotent. In V1.8 a second cancel
        # could interrupt the subprocess-reaping await itself and leave the
        # invocation active for minutes. Once cleanup has started, report the
        # stopping state but never inject another cancellation.
        if inv.state == "stopping":
            snap = inv.snapshot()
            snap["already_stopping"] = True
            return snap

        previous_state = inv.state
        inv.cancel_requested = True
        if previous_state == "waiting":
            # There is no subprocess to reap, so remove it from the visible
            # registry immediately and wake the owner through the cancelled
            # waiter future. _acquire() remains safe if it races this removal.
            self._invocations.pop(inv.invocation_id, None)
            if inv.waiter_future is not None and not inv.waiter_future.done():
                inv.waiter_future.cancel()
            snap = inv.snapshot()
            snap["previous_state"] = previous_state
            snap["state"] = "stopped"
            return snap

        inv.state = "stopping"
        snap = inv.snapshot()
        snap["previous_state"] = previous_state
        target = inv.cancel_task or inv.task
        if target and not target.done():
            target.cancel()
        return snap

    def cancel_all(self) -> list[dict]:
        snapshots = []
        for invocation_id in list(self._invocations):
            snap = self.cancel(invocation_id)
            if snap:
                snapshots.append(snap)
        return snapshots

    def is_idle(self) -> bool:
        s = self.stats()
        return s.active == 0 and s.stopping == 0 and s.waiting == 0
