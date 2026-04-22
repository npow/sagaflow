"""Base workflow state shared by every skillflow skill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ActivityStatus = Literal[
    "pending", "in_progress", "completed", "timed_out", "failed", "spawn_failed"
]

HAIKU_INPUT_PRICE_PER_MILLION = 0.80
HAIKU_OUTPUT_PRICE_PER_MILLION = 4.00
SONNET_INPUT_PRICE_PER_MILLION = 3.00
SONNET_OUTPUT_PRICE_PER_MILLION = 15.00


@dataclass
class ActivityOutcome:
    activity_id: str
    role: str
    status: ActivityStatus
    error_message: str | None = None


@dataclass
class WorkflowState:
    """Base state record. Skill-specific workflows extend this via composition or inheritance."""

    run_id: str
    skill: str
    generation: int = 0
    activity_outcomes: list[ActivityOutcome] = field(default_factory=list)
    cost_running_total: float = 0.0
    terminal_label: str | None = None

    def increment_generation(self) -> int:
        self.generation += 1
        return self.generation

    def record_outcome(self, outcome: ActivityOutcome) -> None:
        self.activity_outcomes.append(outcome)

    def add_cost(self, *, input_tokens: int, output_tokens: int, haiku: bool) -> None:
        if haiku:
            ip = HAIKU_INPUT_PRICE_PER_MILLION
            op = HAIKU_OUTPUT_PRICE_PER_MILLION
        else:
            ip = SONNET_INPUT_PRICE_PER_MILLION
            op = SONNET_OUTPUT_PRICE_PER_MILLION
        self.cost_running_total += (input_tokens / 1_000_000) * ip
        self.cost_running_total += (output_tokens / 1_000_000) * op

    def set_terminal(self, label: str) -> None:
        if self.terminal_label is not None:
            raise RuntimeError(
                f"State {self.run_id} already terminated as {self.terminal_label!r}"
            )
        self.terminal_label = label
