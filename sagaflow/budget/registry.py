"""Process-local enforcer registry keyed by workflow_id."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sagaflow.budget.enforcer import BudgetEnforcer

_ENFORCERS: dict[str, "BudgetEnforcer"] = {}


def register(workflow_id: str, enforcer: "BudgetEnforcer") -> None:
    _ENFORCERS[workflow_id] = enforcer


def get_enforcer(workflow_id: str) -> "BudgetEnforcer | None":
    return _ENFORCERS.get(workflow_id)


def unregister(workflow_id: str) -> None:
    _ENFORCERS.pop(workflow_id, None)
