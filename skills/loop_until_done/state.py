"""State for loop-until-done workflow.

Drives a PRD-generation → falsifiability-judge → execute → verify → review
loop. Each iteration refines acceptance criteria and verifies work done.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sagaflow.durable.state import WorkflowState


@dataclass
class AcceptanceCriterion:
    id: str
    story_id: str
    criterion: str
    verification_command: str
    expected_pattern: str
    passes: bool = False


@dataclass
class Story:
    id: str
    title: str
    acceptance_criteria: list[str] = field(default_factory=list)
    status: str = "pending"


@dataclass
class LoopUntilDoneState(WorkflowState):
    task: str = ""
    max_iter: int = 5
    current_iter: int = 0
    stories: list[Story] = field(default_factory=list)
    criteria: list[AcceptanceCriterion] = field(default_factory=list)
