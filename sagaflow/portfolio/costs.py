"""CostAggregator — per-skill cost queries over arbitrary time windows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from sagaflow.portfolio.db import default_db_path, get_connection
from sagaflow.pricing import RATE_CARD

log = logging.getLogger(__name__)


@dataclass
class TimeWindow:
    start: datetime
    end: datetime

    @classmethod
    def last_days(cls, n: int) -> TimeWindow:
        end = datetime.utcnow()
        start = end - timedelta(days=n)
        return cls(start=start, end=end)


@dataclass
class CostSummary:
    total_usd: float
    avg_usd_per_run: float
    p95_usd_per_run: float
    run_count: int


@dataclass
class CostDataPoint:
    period_start: str
    period_end: str
    total_usd: float
    run_count: int


def estimate_run_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    model_name: str | None,
) -> float:
    in_tok = input_tokens or 0
    out_tok = output_tokens or 0
    if in_tok == 0 and out_tok == 0:
        return 0.0

    model_key = (model_name or "SONNET").upper()
    for card_key in RATE_CARD:
        if card_key in model_key:
            rates = RATE_CARD[card_key]
            return (
                in_tok * rates["input_per_mtok"] / 1_000_000
                + out_tok * rates["output_per_mtok"] / 1_000_000
            )

    rates = RATE_CARD["SONNET"]
    return (
        in_tok * rates["input_per_mtok"] / 1_000_000
        + out_tok * rates["output_per_mtok"] / 1_000_000
    )


class CostAggregator:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_db_path()

    def cost_for_skill(self, skill_name: str, window: TimeWindow) -> CostSummary:
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT input_token_count, output_token_count, model_name "
                "FROM invocations "
                "WHERE skill_name = ? AND started_at >= ? AND started_at <= ?",
                (skill_name, window.start.isoformat(), window.end.isoformat()),
            ).fetchall()
            return self._summarize(rows)
        finally:
            conn.close()

    def cost_by_skill(self, window: TimeWindow) -> list[tuple[str, CostSummary]]:
        conn = get_connection(self._db_path)
        try:
            skills = conn.execute(
                "SELECT DISTINCT skill_name FROM invocations "
                "WHERE started_at >= ? AND started_at <= ?",
                (window.start.isoformat(), window.end.isoformat()),
            ).fetchall()
            result = []
            for row in skills:
                name = row["skill_name"]
                summary = self.cost_for_skill(name, window)
                result.append((name, summary))
            return sorted(result, key=lambda x: x[1].total_usd, reverse=True)
        finally:
            conn.close()

    def cost_trend(
        self,
        skill_name: str,
        granularity: Literal["day", "week", "month"] = "week",
        window: TimeWindow | None = None,
    ) -> list[CostDataPoint]:
        w = window or TimeWindow.last_days(90)
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT started_at, input_token_count, output_token_count, model_name "
                "FROM invocations "
                "WHERE skill_name = ? AND started_at >= ? AND started_at <= ? "
                "ORDER BY started_at",
                (skill_name, w.start.isoformat(), w.end.isoformat()),
            ).fetchall()

            if not rows:
                return []

            buckets: dict[str, list[float]] = {}
            for row in rows:
                dt = datetime.fromisoformat(row["started_at"])
                if granularity == "day":
                    key = dt.strftime("%Y-%m-%d")
                elif granularity == "week":
                    key = dt.strftime("%Y-W%W")
                else:
                    key = dt.strftime("%Y-%m")
                cost = estimate_run_cost(
                    row["input_token_count"],
                    row["output_token_count"],
                    row["model_name"],
                )
                buckets.setdefault(key, []).append(cost)

            return [
                CostDataPoint(
                    period_start=k, period_end=k, total_usd=sum(v), run_count=len(v)
                )
                for k, v in sorted(buckets.items())
            ]
        finally:
            conn.close()

    def _summarize(self, rows: list) -> CostSummary:
        if not rows:
            return CostSummary(
                total_usd=0.0, avg_usd_per_run=0.0, p95_usd_per_run=0.0, run_count=0
            )

        costs = [
            estimate_run_cost(
                r["input_token_count"], r["output_token_count"], r["model_name"]
            )
            for r in rows
        ]
        total = sum(costs)
        avg = total / len(costs)
        sorted_costs = sorted(costs)
        p95_idx = int(len(sorted_costs) * 0.95)
        p95 = sorted_costs[min(p95_idx, len(sorted_costs) - 1)]
        return CostSummary(
            total_usd=total,
            avg_usd_per_run=avg,
            p95_usd_per_run=p95,
            run_count=len(costs),
        )
