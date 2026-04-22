"""State for team-temporal."""

from __future__ import annotations

from dataclasses import dataclass, field

from sagaflow.durable.state import WorkflowState


@dataclass
class TeamState(WorkflowState):
    task: str = ""
    n_workers: int = 2
    current_stage: str = "plan"
    stages_completed: list[str] = field(default_factory=list)
