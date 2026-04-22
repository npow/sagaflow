"""State for deep-debug-temporal."""

from __future__ import annotations

from dataclasses import dataclass, field

from sagaflow.durable.state import WorkflowState


@dataclass
class Hypothesis:
    id: str
    dimension: str
    mechanism: str
    plausibility: str = "pending"  # leading | plausible | rejected


@dataclass
class DeepDebugState(WorkflowState):
    symptom: str = ""
    reproduction_command: str = ""
    max_cycles: int = 3
    current_cycle: int = 0
    hypotheses: list[Hypothesis] = field(default_factory=list)
