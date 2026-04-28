"""ROIScorer — composite ROI scoring for sagaflow skills."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from sagaflow.portfolio.costs import TimeWindow, estimate_run_cost
from sagaflow.portfolio.db import default_db_path, get_connection
from sagaflow.portfolio.outcomes import SIGNAL_WEIGHTS

log = logging.getLogger(__name__)

MIN_SAMPLES = 5
HALF_LIFE_DAYS = 30.0
SCORING_WINDOW_DAYS = 90

WEIGHTS = {
    "usage": 0.25,
    "recency": 0.20,
    "outcome": 0.35,
    "cost_eff": 0.20,
}


class Verdict(Enum):
    THRIVING = "thriving"
    HEALTHY = "healthy"
    AT_RISK = "at_risk"
    DECLINING = "declining"
    CANDIDATE_FOR_RETIREMENT = "candidate_for_retirement"


def _verdict_from_composite(composite: float) -> Verdict:
    if composite >= 0.75:
        return Verdict.THRIVING
    if composite >= 0.50:
        return Verdict.HEALTHY
    if composite >= 0.30:
        return Verdict.AT_RISK
    if composite >= 0.10:
        return Verdict.DECLINING
    return Verdict.CANDIDATE_FOR_RETIREMENT


@dataclass
class ROIScore:
    skill_name: str
    usage_score: float
    recency_score: float
    outcome_score: float | None
    cost_efficiency_score: float | None
    composite: float | None
    verdict: Verdict | None
    insufficient_data: bool
    sample_count: int
    computed_at: datetime


class ROIScorer:
    def __init__(
        self,
        db_path: Path | None = None,
        window_days: int = SCORING_WINDOW_DAYS,
    ) -> None:
        self._db_path = db_path or default_db_path()
        self._window_days = window_days

    def score(self, skill_name: str) -> ROIScore:
        conn = get_connection(self._db_path)
        try:
            return self._compute_score(conn, skill_name)
        finally:
            conn.close()

    def score_all(self) -> list[ROIScore]:
        conn = get_connection(self._db_path)
        try:
            skills = conn.execute(
                "SELECT DISTINCT skill_name FROM invocations"
            ).fetchall()
            return [self._compute_score(conn, row["skill_name"]) for row in skills]
        finally:
            conn.close()

    def _compute_score(self, conn, skill_name: str) -> ROIScore:
        now = datetime.utcnow()
        window = TimeWindow.last_days(self._window_days)

        sample_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM invocations "
            "WHERE skill_name = ? AND source = 'live' AND completion_status = 'success'",
            (skill_name,),
        ).fetchone()
        sample_count = sample_row["cnt"] if sample_row else 0
        insufficient_data = sample_count < MIN_SAMPLES

        usage_score = self._usage_score(conn, skill_name, window)
        recency_score = self._recency_score(conn, skill_name, now)
        outcome_score = self._outcome_score(conn, skill_name)
        cost_efficiency_score = self._cost_efficiency_score(conn, skill_name, window)

        if insufficient_data:
            composite = None
            verdict = None
        else:
            composite = self._weighted_composite(
                usage_score, recency_score, outcome_score, cost_efficiency_score
            )
            verdict = (
                _verdict_from_composite(composite) if composite is not None else None
            )

        return ROIScore(
            skill_name=skill_name,
            usage_score=usage_score,
            recency_score=recency_score,
            outcome_score=outcome_score,
            cost_efficiency_score=cost_efficiency_score,
            composite=composite,
            verdict=verdict,
            insufficient_data=insufficient_data,
            sample_count=sample_count,
            computed_at=now,
        )

    def _usage_score(self, conn, skill_name: str, window: TimeWindow) -> float:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM invocations "
            "WHERE skill_name = ? AND started_at >= ? AND started_at <= ?",
            (skill_name, window.start.isoformat(), window.end.isoformat()),
        ).fetchone()
        count = row["cnt"] if row else 0

        max_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM invocations "
            "WHERE started_at >= ? AND started_at <= ? "
            "GROUP BY skill_name ORDER BY cnt DESC LIMIT 1",
            (window.start.isoformat(), window.end.isoformat()),
        ).fetchone()
        max_count = max_row["cnt"] if max_row else 1
        if max_count == 0:
            return 0.0
        return min(1.0, math.log(1 + count) / math.log(1 + max_count))

    def _recency_score(self, conn, skill_name: str, now: datetime) -> float:
        row = conn.execute(
            "SELECT MAX(completed_at) as last_run FROM invocations WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()
        if not row or not row["last_run"]:
            return 0.0
        last_run = datetime.fromisoformat(row["last_run"])
        days_since = (now - last_run).total_seconds() / 86400.0
        return math.exp(-math.log(2) * days_since / HALF_LIFE_DAYS)

    def _outcome_score(self, conn, skill_name: str) -> float | None:
        rows = conn.execute(
            "SELECT os.signal_type, AVG(os.signal_value) as avg_val "
            "FROM outcome_signals os "
            "JOIN invocations i ON os.invocation_id = i.id "
            "WHERE i.skill_name = ? "
            "GROUP BY os.signal_type",
            (skill_name,),
        ).fetchall()

        if not rows:
            return None

        numerator = 0.0
        denominator = 0.0
        for row in rows:
            weight = SIGNAL_WEIGHTS.get(row["signal_type"], 0.5)
            numerator += row["avg_val"] * weight
            denominator += weight

        if denominator == 0:
            return None
        return numerator / denominator

    def _cost_efficiency_score(
        self, conn, skill_name: str, window: TimeWindow
    ) -> float | None:
        rows = conn.execute(
            "SELECT input_token_count, output_token_count, model_name "
            "FROM invocations "
            "WHERE skill_name = ? AND started_at >= ? AND started_at <= ? "
            "AND input_token_count IS NOT NULL",
            (skill_name, window.start.isoformat(), window.end.isoformat()),
        ).fetchall()
        if not rows:
            return None

        costs = [
            estimate_run_cost(
                r["input_token_count"], r["output_token_count"], r["model_name"]
            )
            for r in rows
        ]
        avg_cost = sum(costs) / len(costs)

        all_rows = conn.execute(
            "SELECT input_token_count, output_token_count, model_name "
            "FROM invocations "
            "WHERE started_at >= ? AND started_at <= ? "
            "AND input_token_count IS NOT NULL",
            (window.start.isoformat(), window.end.isoformat()),
        ).fetchall()
        if not all_rows:
            return None

        all_costs = sorted(
            estimate_run_cost(
                r["input_token_count"], r["output_token_count"], r["model_name"]
            )
            for r in all_rows
        )
        p95_idx = int(len(all_costs) * 0.95)
        ref_cost = max(1e-6, all_costs[min(p95_idx, len(all_costs) - 1)])

        return max(0.0, min(1.0, 1.0 - (avg_cost / ref_cost)))

    @staticmethod
    def _weighted_composite(
        usage: float,
        recency: float,
        outcome: float | None,
        cost_eff: float | None,
    ) -> float | None:
        scores = {
            "usage": usage,
            "recency": recency,
            "outcome": outcome,
            "cost_eff": cost_eff,
        }
        active = {k: v for k, v in scores.items() if v is not None}
        if not active:
            return None

        total_weight = sum(WEIGHTS[k] for k in active)
        if total_weight == 0:
            return None

        return sum(scores[k] * WEIGHTS[k] / total_weight for k in active)
