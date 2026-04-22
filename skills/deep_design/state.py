"""State for deep-design-temporal workflow.

Minimum-viable state for the design specification critique loop. Tracks the
current spec draft, flaw findings from critics, and round progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sagaflow.durable.state import WorkflowState

FlawSeverity = Literal["critical", "major", "minor"]
FlawStatus = Literal["open", "addressed", "wont_fix"]


@dataclass
class Flaw:
    id: str
    title: str
    severity: FlawSeverity
    dimension: str
    scenario: str
    status: FlawStatus = "open"


@dataclass
class DeepDesignState(WorkflowState):
    core_claim: str = ""
    max_rounds: int = 3
    current_round: int = 0
    spec_draft: str = ""
    flaws: list[Flaw] = field(default_factory=list)
