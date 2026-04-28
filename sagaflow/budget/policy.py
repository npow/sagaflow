"""BudgetPolicy dataclass and PolicyLoader."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BudgetPolicy:
    max_cost_usd: float | None = None
    downgrade_threshold: float = 0.85
    alert_thresholds: list[float] = field(default_factory=lambda: [0.5, 0.8, 1.0])
    tier_profile: dict[str, str] = field(default_factory=dict)
    downgrade_ladder: list[str] = field(
        default_factory=lambda: ["OPUS", "SONNET", "HAIKU"]
    )
    hard_stop: bool = True


class PolicyLoader:
    @staticmethod
    def load(
        skill_dir: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> BudgetPolicy | None:
        """Load budget policy from config sources.

        Priority (highest wins):
        1. overrides dict (from CLI --budget-usd / --budget-policy)
        2. SKILL.budget.yaml in skill_dir
        3. budget: block in SKILL.md front-matter
        4. None (no enforcement)

        On parse error: logs warning and returns None.
        """
        base: dict[str, Any] = {}

        if skill_dir is not None:
            base = _load_from_skill_dir(skill_dir)

        if overrides:
            base.update(overrides)

        if not base:
            return None

        try:
            return _dict_to_policy(base)
        except Exception:
            logger.warning("failed to parse budget policy — enforcement disabled", exc_info=True)
            return None


def _load_from_skill_dir(skill_dir: Path) -> dict[str, Any]:
    """Try SKILL.budget.yaml first, then SKILL.md front-matter."""
    yaml_path = skill_dir / "SKILL.budget.yaml"
    if yaml_path.exists():
        return _parse_yaml(yaml_path)

    md_path = skill_dir / "SKILL.md"
    if md_path.exists():
        return _extract_budget_frontmatter(md_path)

    return {}


def _parse_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        logger.warning("failed to parse %s — skipping", path, exc_info=True)
    return {}


def _extract_budget_frontmatter(md_path: Path) -> dict[str, Any]:
    """Extract budget: block from SKILL.md YAML front-matter."""
    try:
        text = md_path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}
        end = text.index("---", 3)
        front = text[3:end]
        import yaml
        data = yaml.safe_load(front)
        if isinstance(data, dict) and "budget" in data:
            budget = data["budget"]
            if isinstance(budget, dict):
                return budget
    except Exception:
        logger.warning("failed to extract budget from %s", md_path, exc_info=True)
    return {}


def _dict_to_policy(d: dict[str, Any]) -> BudgetPolicy:
    policy = BudgetPolicy()
    if "max_cost_usd" in d:
        policy.max_cost_usd = float(d["max_cost_usd"])
    if "downgrade_threshold" in d:
        policy.downgrade_threshold = float(d["downgrade_threshold"])
    if "alert_thresholds" in d:
        policy.alert_thresholds = [float(t) for t in d["alert_thresholds"]]
    if "tier_profile" in d:
        policy.tier_profile = {str(k): str(v).upper() for k, v in d["tier_profile"].items()}
    if "downgrade_ladder" in d:
        policy.downgrade_ladder = [str(t).upper() for t in d["downgrade_ladder"]]
    if "hard_stop" in d:
        policy.hard_stop = bool(d["hard_stop"])
    return policy
