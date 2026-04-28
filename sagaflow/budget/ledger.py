"""BudgetLedger — in-memory spend accumulator for budget enforcement."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from sagaflow.budget.policy import BudgetPolicy

logger = logging.getLogger(__name__)


class BudgetDecision(str, Enum):
    ALLOW = "allow"
    DOWNGRADE = "downgrade"
    ABORT = "abort"


@dataclass
class BudgetStatus:
    decision: BudgetDecision
    current_cost_usd: float
    budget_usd: float | None
    fraction: float | None
    recommended_tier: str | None
    message: str


@dataclass
class BudgetLedger:
    policy: BudgetPolicy
    accumulated_cost_usd: float = 0.0
    step_count: int = 0
    alerts_fired: set[float] = field(default_factory=set)

    @classmethod
    def from_manifest_or_fresh(
        cls, policy: BudgetPolicy, manifest_path: Path | None
    ) -> BudgetLedger:
        """Reconstruct ledger from manifest on resume, or start fresh."""
        if manifest_path and manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                br = data.get("budget_result", {})
                if br:
                    return cls(
                        policy=policy,
                        accumulated_cost_usd=br.get("final_cost_usd", 0.0),
                        step_count=br.get("step_count", 0),
                        alerts_fired=set(br.get("alerts_fired", [])),
                    )
            except Exception:
                logger.warning("failed to read manifest for budget recovery", exc_info=True)
        return cls(policy=policy)

    def record_step(self, cost_usd: float) -> None:
        self.accumulated_cost_usd += cost_usd
        self.step_count += 1

    @property
    def budget_fraction(self) -> float | None:
        if self.policy.max_cost_usd is None:
            return None
        if self.policy.max_cost_usd == 0:
            return float("inf") if self.accumulated_cost_usd > 0 else 0.0
        return self.accumulated_cost_usd / self.policy.max_cost_usd

    def check(self) -> BudgetStatus:
        if self.policy.max_cost_usd is None:
            return BudgetStatus(
                decision=BudgetDecision.ALLOW,
                current_cost_usd=self.accumulated_cost_usd,
                budget_usd=None,
                fraction=None,
                recommended_tier=None,
                message="No budget configured.",
            )
        fraction = self.budget_fraction
        assert fraction is not None  # guarded by max_cost_usd check above
        if fraction >= 1.0 and self.policy.hard_stop:
            return BudgetStatus(
                decision=BudgetDecision.ABORT,
                current_cost_usd=self.accumulated_cost_usd,
                budget_usd=self.policy.max_cost_usd,
                fraction=fraction,
                recommended_tier=None,
                message=f"Budget exceeded: {self.accumulated_cost_usd:.4f} USD of "
                f"{self.policy.max_cost_usd:.2f} USD.",
            )
        if fraction >= self.policy.downgrade_threshold:
            return BudgetStatus(
                decision=BudgetDecision.DOWNGRADE,
                current_cost_usd=self.accumulated_cost_usd,
                budget_usd=self.policy.max_cost_usd,
                fraction=fraction,
                recommended_tier=None,
                message=f"At {fraction:.0%} of budget — downgrading tier.",
            )
        return BudgetStatus(
            decision=BudgetDecision.ALLOW,
            current_cost_usd=self.accumulated_cost_usd,
            budget_usd=self.policy.max_cost_usd,
            fraction=fraction,
            recommended_tier=None,
            message="Within budget.",
        )
