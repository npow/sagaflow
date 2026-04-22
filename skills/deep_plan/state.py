"""State for deep-plan-temporal workflow."""

from __future__ import annotations

from dataclasses import dataclass, field

from sagaflow.durable.state import WorkflowState


@dataclass
class DeepPlanState(WorkflowState):
    task: str = ""
    max_iter: int = 5
    current_iter: int = 0
    verdict_history: list[str] = field(default_factory=list)
