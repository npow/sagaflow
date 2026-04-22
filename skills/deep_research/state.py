"""State for deep-research-temporal."""

from __future__ import annotations

from dataclasses import dataclass, field

from sagaflow.durable.state import WorkflowState


@dataclass
class Direction:
    id: str
    question: str
    dimension: str
    status: str = "frontier"
    findings_path: str = ""


@dataclass
class DeepResearchState(WorkflowState):
    seed: str = ""
    max_rounds: int = 2
    current_round: int = 0
    frontier: list[Direction] = field(default_factory=list)
