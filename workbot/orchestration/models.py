from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class WorkflowStep:
    step_id: str
    executor: str  # windows | remote
    instruction: str
    node: str | None = None
    depends_on: list[str] = field(default_factory=list)
    notify_on_complete: bool = False
    milestone: str = ""


@dataclass(slots=True)
class WorkflowPlan:
    summary: str
    steps: list[WorkflowStep]
