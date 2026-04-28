"""BudgetEnforcer — single enforcement point called per spawn_subagent."""

from __future__ import annotations

from sagaflow.budget.ledger import BudgetDecision, BudgetLedger, BudgetStatus
from sagaflow.budget.router import TierRouter


class BudgetExceededError(Exception):
    """Raised when budget is exceeded and hard_stop is True."""


class BudgetEnforcer:
    def __init__(self, ledger: BudgetLedger, router: TierRouter) -> None:
        self._ledger = ledger
        self._router = router

    def pre_dispatch(
        self,
        role: str,
        requested_tier: str,
    ) -> tuple[str, BudgetStatus]:
        """Check budget before dispatching a subagent.

        Returns (resolved_tier, status).
        Caller must raise BudgetExceededError if status.decision == ABORT.
        """
        status = self._ledger.check()
        if status.decision == BudgetDecision.ABORT:
            return requested_tier, status
        resolved = self._router.resolve(role, requested_tier, status)
        return resolved, status

    def record_cost(self, cost_usd: float) -> list[float]:
        """Record actual cost and return newly crossed alert thresholds."""
        prev_fraction = self._ledger.budget_fraction
        self._ledger.record_step(cost_usd)
        newly_crossed = self._compute_new_alerts(prev_fraction)
        for t in newly_crossed:
            self._ledger.alerts_fired.add(t)
        return newly_crossed

    def _compute_new_alerts(self, prev_fraction: float | None) -> list[float]:
        curr = self._ledger.budget_fraction
        if curr is None:
            return []
        return [
            t
            for t in self._ledger.policy.alert_thresholds
            if t not in self._ledger.alerts_fired
            and (prev_fraction is None or prev_fraction < t)
            and curr >= t
        ]

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger
