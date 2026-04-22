"""State for deep-qa-temporal workflow.

Minimum-viable port of the file-based deep-qa state machine. Focuses on what
Temporal needs to drive the workflow: frontier, defects, round counter, and a
termination label. Deferred for a later version: two-pass judges,
rationalization auditor, coverage per required-category dimension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sagaflow.durable.state import WorkflowState

ArtifactType = Literal["doc", "code", "research", "skill"]
DefectSeverity = Literal["critical", "major", "minor"]
AngleStatus = Literal["frontier", "in_progress", "explored", "timed_out"]


@dataclass
class Angle:
    id: str
    question: str
    dimension: str
    status: AngleStatus = "frontier"
    critique_path: str = ""


@dataclass
class Defect:
    id: str
    title: str
    severity: DefectSeverity
    dimension: str
    scenario: str
    root_cause: str
    source_angle_id: str


@dataclass
class DeepQaState(WorkflowState):
    artifact_path: str = ""
    artifact_type: ArtifactType = "doc"
    max_rounds: int = 3
    hard_stop: int = 6  # 2 × max_rounds default
    current_round: int = 0
    frontier: list[Angle] = field(default_factory=list)
    explored_angles: list[Angle] = field(default_factory=list)
    defects: list[Defect] = field(default_factory=list)
