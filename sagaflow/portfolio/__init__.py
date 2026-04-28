"""Portfolio Evaluation Framework — passive observability and ROI scoring for sagaflow skills."""

from __future__ import annotations

from sagaflow.portfolio.costs import CostAggregator, CostDataPoint, CostSummary, TimeWindow
from sagaflow.portfolio.db import db_exists, default_db_path, get_connection, init_db
from sagaflow.portfolio.outcomes import CollectionSummary, OutcomeCollector
from sagaflow.portfolio.retirement import RetirementAdvisor, RetirementRecommendation
from sagaflow.portfolio.scorer import ROIScore, ROIScorer, Verdict
from sagaflow.portfolio.telemetry import (
    InvocationRecord,
    NullTelemetryWriter,
    TelemetryWriter,
    get_writer,
)

__all__ = [
    "CostAggregator",
    "CostDataPoint",
    "CostSummary",
    "CollectionSummary",
    "InvocationRecord",
    "NullTelemetryWriter",
    "OutcomeCollector",
    "ROIScore",
    "ROIScorer",
    "RetirementAdvisor",
    "RetirementRecommendation",
    "TelemetryWriter",
    "TimeWindow",
    "Verdict",
    "db_exists",
    "default_db_path",
    "get_connection",
    "get_writer",
    "init_db",
]
