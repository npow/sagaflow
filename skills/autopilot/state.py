"""State for autopilot-temporal."""

from __future__ import annotations

from dataclasses import dataclass, field

from sagaflow.durable.state import WorkflowState


@dataclass
class AutopilotState(WorkflowState):
    initial_idea: str = ""
    current_phase: str = "expand"
    phases_passed: list[str] = field(default_factory=list)
