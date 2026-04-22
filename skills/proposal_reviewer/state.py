"""State types for the proposal-reviewer skill."""

from __future__ import annotations

from dataclasses import dataclass, field

from sagaflow.durable.state import WorkflowState


@dataclass
class Claim:
    id: str
    text: str
    tier: str  # "core" | "supporting" | "peripheral"


@dataclass
class Weakness:
    id: str
    title: str
    severity: str   # "high" | "medium" | "low"
    dimension: str  # "viability" | "competition" | "structural" | "evidence"
    scenario: str


@dataclass
class ProposalReviewState(WorkflowState):
    proposal_text: str = ""
    core_claim: str = ""
    claims: list[Claim] = field(default_factory=list)
    weaknesses: list[Weakness] = field(default_factory=list)
