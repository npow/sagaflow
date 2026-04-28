"""RetirementAdvisor — advisory-only skill retirement recommendations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from sagaflow.portfolio.db import default_db_path, get_connection
from sagaflow.portfolio.scorer import ROIScorer, Verdict

log = logging.getLogger(__name__)

UNUSED_DAYS_THRESHOLD = 90
CONSECUTIVE_DECLINING_PERIODS = 3
EXPERIMENTAL_UNUSED_DAYS_THRESHOLD = 30


@dataclass
class RetirementRecommendation:
    skill_name: str
    criterion_triggered: str
    recommended_transition: str
    confidence: Literal["low", "medium", "high"]
    narrative: str
    generated_at: str


class RetirementAdvisor:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_db_path()
        self._scorer = ROIScorer(db_path=db_path)

    def candidates(self) -> list[RetirementRecommendation]:
        results: list[RetirementRecommendation] = []
        conn = get_connection(self._db_path)
        try:
            skills = conn.execute(
                "SELECT DISTINCT skill_name FROM invocations"
            ).fetchall()
            for row in skills:
                rec = self._evaluate(conn, row["skill_name"])
                if rec:
                    results.append(rec)
        finally:
            conn.close()
        return results

    def recommendation_for(self, skill_name: str) -> RetirementRecommendation | None:
        conn = get_connection(self._db_path)
        try:
            return self._evaluate(conn, skill_name)
        finally:
            conn.close()

    def _evaluate(self, conn, skill_name: str) -> RetirementRecommendation | None:
        now = datetime.utcnow()
        generated_at = now.isoformat()

        last_row = conn.execute(
            "SELECT MAX(completed_at) as last_run FROM invocations "
            "WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()

        if last_row and last_row["last_run"]:
            last_run = datetime.fromisoformat(last_row["last_run"])
            days_unused = (now - last_run).days

            if days_unused >= UNUSED_DAYS_THRESHOLD:
                return RetirementRecommendation(
                    skill_name=skill_name,
                    criterion_triggered="unused_days",
                    recommended_transition="deprecated",
                    confidence="high",
                    narrative=f"{skill_name} has not been invoked for {days_unused} days.",
                    generated_at=generated_at,
                )

            if self._is_experimental(skill_name):
                if days_unused >= EXPERIMENTAL_UNUSED_DAYS_THRESHOLD:
                    return RetirementRecommendation(
                        skill_name=skill_name,
                        criterion_triggered="stale_experimental",
                        recommended_transition="deleted",
                        confidence="medium",
                        narrative=(
                            f"Experimental skill {skill_name} unused "
                            f"for {days_unused} days."
                        ),
                        generated_at=generated_at,
                    )

        score = self._scorer._compute_score(conn, skill_name)
        if score.verdict == Verdict.CANDIDATE_FOR_RETIREMENT:
            return RetirementRecommendation(
                skill_name=skill_name,
                criterion_triggered="declining_verdicts",
                recommended_transition="deprecated",
                confidence="medium",
                narrative=(
                    f"{skill_name} scored {score.composite:.2f} "
                    f"(candidate for retirement)."
                ),
                generated_at=generated_at,
            )

        return None

    @staticmethod
    def _is_experimental(skill_name: str) -> bool:
        try:
            from sagaflow.catalog import build_catalog

            catalog = build_catalog()
            entry = catalog.show(skill_name)
            if entry and entry.maturity:
                return entry.maturity == "experimental"
        except Exception:
            pass
        return False
