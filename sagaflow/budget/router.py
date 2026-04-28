"""TierRouter — resolves model tier given role + budget status."""

from __future__ import annotations

from sagaflow.budget.ledger import BudgetDecision, BudgetStatus
from sagaflow.budget.policy import BudgetPolicy


class TierRouter:
    def __init__(self, policy: BudgetPolicy) -> None:
        self._policy = policy
        self._ladder = policy.downgrade_ladder

    def resolve(self, role: str, requested_tier: str, status: BudgetStatus) -> str:
        """Resolve the model tier for a given role.

        1. Apply tier_profile override for role if present.
        2. If status is DOWNGRADE, walk ladder one step cheaper.
        3. Clamp at cheapest ladder entry.
        """
        tier = self._policy.tier_profile.get(role, requested_tier)
        if status.decision == BudgetDecision.DOWNGRADE:
            tier = self._downgrade_one_step(tier)
        return tier

    def _downgrade_one_step(self, tier: str) -> str:
        try:
            idx = self._ladder.index(tier)
        except ValueError:
            return tier
        return self._ladder[min(idx + 1, len(self._ladder) - 1)]
