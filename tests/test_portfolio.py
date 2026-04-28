"""Tests for sagaflow.portfolio — db, telemetry, outcomes, costs, scorer, retirement, CLI."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from sagaflow.cli import main
from sagaflow.portfolio.costs import CostAggregator, TimeWindow, estimate_run_cost
from sagaflow.portfolio.db import (
    CURRENT_SCHEMA_VERSION,
    db_exists,
    default_db_path,
    get_connection,
    init_db,
)
from sagaflow.portfolio.outcomes import (
    BV_VERDICT_SCORES,
    SIGNAL_WEIGHTS,
    OutcomeCollector,
)
from sagaflow.portfolio.retirement import RetirementAdvisor
from sagaflow.portfolio.scorer import ROIScorer, Verdict, _verdict_from_composite
from sagaflow.portfolio.telemetry import (
    InvocationRecord,
    NullTelemetryWriter,
    TelemetryWriter,
    get_writer,
    reset_writer,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return init_db(tmp_path / "portfolio.db")


def _insert_invocation(
    conn: sqlite3.Connection,
    *,
    run_id: str = "run-1",
    skill_name: str = "deep-qa",
    started_at: str | None = None,
    completed_at: str | None = None,
    status: str = "success",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    model: str = "SONNET",
    source: str = "live",
    artifact_path: str | None = None,
    slack_msg_id: str | None = None,
) -> int:
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO invocations "
        "(run_id, skill_name, started_at, completed_at, completion_status, "
        "input_token_count, output_token_count, model_name, source, "
        "output_artifact_path, slack_message_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            skill_name,
            started_at or now,
            completed_at or now,
            status,
            input_tokens,
            output_tokens,
            model,
            source,
            artifact_path,
            slack_msg_id,
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ---------------------------------------------------------------------------
# db.py
# ---------------------------------------------------------------------------


class TestDB:
    def test_init_creates_db(self, tmp_path: Path) -> None:
        path = init_db(tmp_path / "test.db")
        assert path.exists()

    def test_init_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "test.db"
        init_db(path)
        init_db(path)
        conn = sqlite3.connect(str(path))
        versions = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        conn.close()
        assert versions == 1

    def test_schema_version(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row["version"] == CURRENT_SCHEMA_VERSION

    def test_get_connection_missing_db(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Portfolio DB not found"):
            get_connection(tmp_path / "nope.db")

    def test_db_exists(self, tmp_path: Path) -> None:
        assert not db_exists(tmp_path / "nope.db")
        init_db(tmp_path / "test.db")
        assert db_exists(tmp_path / "test.db")

    def test_wal_mode(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_tables_exist(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "invocations" in tables
        assert "outcome_signals" in tables
        assert "cost_snapshots" in tables
        assert "lifecycle_events" in tables
        assert "schema_version" in tables


# ---------------------------------------------------------------------------
# telemetry.py
# ---------------------------------------------------------------------------


class TestTelemetry:
    def test_writer_writes_record(self, db_path: Path) -> None:
        writer = TelemetryWriter(db_path=db_path)
        record = InvocationRecord(
            run_id="tel-1",
            skill_name="deep-qa",
            trigger_context="test",
            invoker_type="test",
            started_at=datetime.utcnow().isoformat(),
            completed_at=datetime.utcnow().isoformat(),
            completion_status="success",
            input_token_count=100,
            output_token_count=50,
            model_name="SONNET",
        )
        writer.enqueue(record)
        writer.flush()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM invocations WHERE run_id = ?", ("tel-1",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["skill_name"] == "deep-qa"

    def test_null_writer_does_nothing(self) -> None:
        writer = NullTelemetryWriter()
        record = InvocationRecord(
            run_id="null-1",
            skill_name="test",
            trigger_context="test",
            invoker_type="test",
            started_at=datetime.utcnow().isoformat(),
            completed_at=datetime.utcnow().isoformat(),
            completion_status="success",
        )
        writer.enqueue(record)
        writer.flush()

    def test_get_writer_returns_null_when_no_db(self, tmp_path: Path) -> None:
        reset_writer()
        with patch(
            "sagaflow.portfolio.telemetry.default_db_path",
            return_value=tmp_path / "nonexistent.db",
        ):
            w = get_writer()
            assert isinstance(w, NullTelemetryWriter)
        reset_writer()

    def test_invocation_record_defaults(self) -> None:
        rec = InvocationRecord(
            run_id="r", skill_name="s",
            trigger_context="t", invoker_type="i",
            started_at="2026-01-01", completed_at="2026-01-01",
            completion_status="success",
        )
        assert rec.input_token_count is None
        assert rec.source == "live"


# ---------------------------------------------------------------------------
# costs.py
# ---------------------------------------------------------------------------


class TestCosts:
    def test_estimate_run_cost_sonnet(self) -> None:
        cost = estimate_run_cost(1_000_000, 500_000, "SONNET")
        assert cost == pytest.approx(3.0 + 7.5, rel=1e-4)

    def test_estimate_run_cost_haiku(self) -> None:
        cost = estimate_run_cost(1_000_000, 1_000_000, "HAIKU")
        assert cost == pytest.approx(0.8 + 4.0, rel=1e-4)

    def test_estimate_run_cost_unknown_model_defaults_sonnet(self) -> None:
        cost = estimate_run_cost(1_000_000, 0, "UNKNOWN_MODEL")
        expected = estimate_run_cost(1_000_000, 0, "SONNET")
        assert cost == pytest.approx(expected, rel=1e-4)

    def test_estimate_run_cost_zeros(self) -> None:
        assert estimate_run_cost(0, 0, "SONNET") == 0.0
        assert estimate_run_cost(None, None, None) == 0.0

    def test_time_window_last_days(self) -> None:
        tw = TimeWindow.last_days(30)
        delta = tw.end - tw.start
        assert 29 <= delta.days <= 30

    def test_cost_for_skill(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="c1", skill_name="deep-qa")
        _insert_invocation(conn, run_id="c2", skill_name="deep-qa")
        conn.close()

        agg = CostAggregator(db_path=db_path)
        tw = TimeWindow.last_days(7)
        summary = agg.cost_for_skill("deep-qa", tw)
        assert summary.run_count == 2
        assert summary.total_usd > 0
        assert summary.avg_usd_per_run > 0

    def test_cost_by_skill(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="c1", skill_name="deep-qa")
        _insert_invocation(conn, run_id="c2", skill_name="deep-design")
        conn.close()

        agg = CostAggregator(db_path=db_path)
        result = agg.cost_by_skill(TimeWindow.last_days(7))
        assert len(result) == 2

    def test_cost_trend(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="c1", skill_name="deep-qa")
        conn.close()

        agg = CostAggregator(db_path=db_path)
        points = agg.cost_trend("deep-qa", granularity="day")
        assert len(points) >= 1
        assert points[0].run_count == 1

    def test_cost_for_empty_skill(self, db_path: Path) -> None:
        agg = CostAggregator(db_path=db_path)
        summary = agg.cost_for_skill("nonexistent", TimeWindow.last_days(7))
        assert summary.run_count == 0
        assert summary.total_usd == 0.0


# ---------------------------------------------------------------------------
# outcomes.py
# ---------------------------------------------------------------------------


class TestOutcomes:
    def test_signal_weights_defined(self) -> None:
        assert "completed_successfully" in SIGNAL_WEIGHTS
        assert "artifact_present" in SIGNAL_WEIGHTS
        assert "bv_verdict" in SIGNAL_WEIGHTS

    def test_bv_verdict_scores(self) -> None:
        assert BV_VERDICT_SCORES["IDENTICAL"] == 1.0
        assert BV_VERDICT_SCORES["REGRESSION"] == 0.2

    def test_collect_success_signal(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="o1", status="success")
        conn.close()

        collector = OutcomeCollector(db_path=db_path)
        summary = collector.collect()
        assert summary.processed == 1
        assert summary.signals_written >= 1

        conn = get_connection(db_path)
        signals = conn.execute(
            "SELECT signal_type, signal_value FROM outcome_signals"
        ).fetchall()
        conn.close()
        signal_map = {s["signal_type"]: s["signal_value"] for s in signals}
        assert signal_map["completed_successfully"] == 1.0

    def test_collect_failure_signal(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="o2", status="failure")
        conn.close()

        collector = OutcomeCollector(db_path=db_path)
        collector.collect()

        conn = get_connection(db_path)
        signals = conn.execute(
            "SELECT signal_type, signal_value FROM outcome_signals"
        ).fetchall()
        conn.close()
        signal_map = {s["signal_type"]: s["signal_value"] for s in signals}
        assert signal_map["completed_successfully"] == 0.0

    def test_collect_skips_already_collected(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="o3")
        conn.close()

        collector = OutcomeCollector(db_path=db_path)
        s1 = collector.collect()
        s2 = collector.collect()
        assert s1.processed == 1
        assert s2.processed == 0

    def test_collect_artifact_signal(self, db_path: Path, tmp_path: Path) -> None:
        artifact = tmp_path / "sagaflow" / "report.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("x" * 200)

        conn = get_connection(db_path)
        _insert_invocation(
            conn, run_id="o4", artifact_path="report.md"
        )
        conn.close()

        collector = OutcomeCollector(db_path=db_path, sagaflow_root=tmp_path / "sagaflow")
        collector.collect()

        conn = get_connection(db_path)
        signals = conn.execute(
            "SELECT signal_type, signal_value FROM outcome_signals"
        ).fetchall()
        conn.close()
        signal_map = {s["signal_type"]: s["signal_value"] for s in signals}
        assert signal_map.get("artifact_present") == 1.0


# ---------------------------------------------------------------------------
# scorer.py
# ---------------------------------------------------------------------------


class TestScorer:
    def test_verdict_from_composite(self) -> None:
        assert _verdict_from_composite(0.80) == Verdict.THRIVING
        assert _verdict_from_composite(0.60) == Verdict.HEALTHY
        assert _verdict_from_composite(0.35) == Verdict.AT_RISK
        assert _verdict_from_composite(0.15) == Verdict.DECLINING
        assert _verdict_from_composite(0.05) == Verdict.CANDIDATE_FOR_RETIREMENT

    def test_verdict_boundaries(self) -> None:
        assert _verdict_from_composite(0.75) == Verdict.THRIVING
        assert _verdict_from_composite(0.50) == Verdict.HEALTHY
        assert _verdict_from_composite(0.30) == Verdict.AT_RISK
        assert _verdict_from_composite(0.10) == Verdict.DECLINING
        assert _verdict_from_composite(0.0) == Verdict.CANDIDATE_FOR_RETIREMENT

    def test_insufficient_data(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="s1", skill_name="test-skill")
        conn.close()

        scorer = ROIScorer(db_path=db_path)
        score = scorer.score("test-skill")
        assert score.insufficient_data is True
        assert score.composite is None
        assert score.verdict is None
        assert score.sample_count == 1

    def test_sufficient_data_produces_verdict(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        for i in range(6):
            _insert_invocation(
                conn, run_id=f"s{i}", skill_name="active-skill",
                input_tokens=1000, output_tokens=500,
            )
        conn.close()

        scorer = ROIScorer(db_path=db_path)
        score = scorer.score("active-skill")
        assert score.insufficient_data is False
        assert score.composite is not None
        assert score.verdict is not None
        assert 0.0 <= score.composite <= 1.0

    def test_score_all(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        for i in range(6):
            _insert_invocation(conn, run_id=f"a{i}", skill_name="skill-a")
            _insert_invocation(conn, run_id=f"b{i}", skill_name="skill-b")
        conn.close()

        scorer = ROIScorer(db_path=db_path)
        scores = scorer.score_all()
        assert len(scores) == 2
        names = {s.skill_name for s in scores}
        assert names == {"skill-a", "skill-b"}

    def test_recency_decays(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        old_date = (datetime.utcnow() - timedelta(days=60)).isoformat()
        _insert_invocation(
            conn, run_id="old1", skill_name="old-skill",
            started_at=old_date, completed_at=old_date,
        )
        _insert_invocation(conn, run_id="new1", skill_name="new-skill")
        conn.close()

        scorer = ROIScorer(db_path=db_path)
        old_score = scorer.score("old-skill")
        new_score = scorer.score("new-skill")
        assert old_score.recency_score < new_score.recency_score

    def test_usage_score_normalization(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        for i in range(10):
            _insert_invocation(conn, run_id=f"heavy-{i}", skill_name="heavy")
        _insert_invocation(conn, run_id="light-0", skill_name="light")
        conn.close()

        scorer = ROIScorer(db_path=db_path)
        heavy = scorer.score("heavy")
        light = scorer.score("light")
        assert heavy.usage_score > light.usage_score


# ---------------------------------------------------------------------------
# retirement.py
# ---------------------------------------------------------------------------


class TestRetirement:
    def test_unused_skill_flagged(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        old_date = (datetime.utcnow() - timedelta(days=100)).isoformat()
        _insert_invocation(
            conn, run_id="ret1", skill_name="stale-skill",
            started_at=old_date, completed_at=old_date,
        )
        conn.close()

        advisor = RetirementAdvisor(db_path=db_path)
        rec = advisor.recommendation_for("stale-skill")
        assert rec is not None
        assert rec.criterion_triggered == "unused_days"
        assert rec.recommended_transition == "deprecated"

    def test_recent_skill_not_flagged(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="ret2", skill_name="active-skill")
        conn.close()

        advisor = RetirementAdvisor(db_path=db_path)
        rec = advisor.recommendation_for("active-skill")
        assert rec is None

    def test_candidates_returns_list(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        old_date = (datetime.utcnow() - timedelta(days=100)).isoformat()
        _insert_invocation(
            conn, run_id="ret3", skill_name="old1",
            started_at=old_date, completed_at=old_date,
        )
        _insert_invocation(conn, run_id="ret4", skill_name="fresh1")
        conn.close()

        advisor = RetirementAdvisor(db_path=db_path)
        candidates = advisor.candidates()
        names = {c.skill_name for c in candidates}
        assert "old1" in names
        assert "fresh1" not in names


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

_DB_PATH_TARGETS = [
    "sagaflow.portfolio.db.default_db_path",
    "sagaflow.portfolio.scorer.default_db_path",
    "sagaflow.portfolio.costs.default_db_path",
    "sagaflow.portfolio.retirement.default_db_path",
    "sagaflow.portfolio.outcomes.default_db_path",
]


def _patch_db_path(db_path: Path) -> ExitStack:
    stack = ExitStack()
    for target in _DB_PATH_TARGETS:
        stack.enter_context(patch(target, return_value=db_path))
    return stack


class TestCLI:
    def test_portfolio_init(self, tmp_path: Path) -> None:
        db = tmp_path / "portfolio.db"
        runner = CliRunner()
        with _patch_db_path(db):
            result = runner.invoke(main, ["portfolio", "init"])
        assert result.exit_code == 0
        assert "initialized" in result.output.lower()
        assert db.exists()

    def test_portfolio_summary_no_db(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with _patch_db_path(tmp_path / "nope.db"):
            result = runner.invoke(main, ["portfolio", "summary"])
        assert result.exit_code != 0

    def test_portfolio_summary_with_data(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        for i in range(6):
            _insert_invocation(conn, run_id=f"cli-{i}", skill_name="my-skill")
        conn.close()

        runner = CliRunner()
        with _patch_db_path(db_path):
            result = runner.invoke(main, ["portfolio", "summary"])
        assert result.exit_code == 0
        assert "my-skill" in result.output

    def test_portfolio_inspect(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        for i in range(6):
            _insert_invocation(conn, run_id=f"insp-{i}", skill_name="inspect-me")
        conn.close()

        runner = CliRunner()
        with _patch_db_path(db_path):
            result = runner.invoke(main, ["portfolio", "inspect", "inspect-me"])
        assert result.exit_code == 0
        assert "inspect-me" in result.output
        assert "usage" in result.output.lower()

    def test_portfolio_trends(self, db_path: Path) -> None:
        conn = get_connection(db_path)
        _insert_invocation(conn, run_id="t1", skill_name="trend-skill")
        conn.close()

        runner = CliRunner()
        with _patch_db_path(db_path):
            result = runner.invoke(main, ["portfolio", "trends", "--skill", "trend-skill"])
        assert result.exit_code == 0

    def test_portfolio_retire_dry_run(self, db_path: Path) -> None:
        runner = CliRunner()
        with _patch_db_path(db_path):
            result = runner.invoke(
                main,
                ["portfolio", "retire", "old-skill", "--transition", "deprecated", "--dry-run"],
            )
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()

    def test_portfolio_retire_writes_event(self, db_path: Path) -> None:
        runner = CliRunner()
        with _patch_db_path(db_path):
            result = runner.invoke(
                main,
                ["portfolio", "retire", "old-skill", "--transition", "deprecated"],
            )
        assert result.exit_code == 0

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM lifecycle_events WHERE skill_name = ?", ("old-skill",)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["to_state"] == "deprecated"

    def test_portfolio_snapshot_and_regress(self, db_path: Path, tmp_path: Path) -> None:
        conn = get_connection(db_path)
        for i in range(6):
            _insert_invocation(conn, run_id=f"snap-{i}", skill_name="snap-skill")
        conn.close()

        snap_dir = tmp_path / ".sagaflow" / "portfolio_snapshots"
        runner = CliRunner()

        with _patch_db_path(db_path) as stack:
            stack.enter_context(patch("sagaflow.cli.Path.home", return_value=tmp_path))
            result = runner.invoke(
                main, ["portfolio", "snapshot", "--name", "test-baseline"]
            )
        assert result.exit_code == 0
        assert snap_dir.exists()
        snap_file = snap_dir / "test-baseline.json"
        assert snap_file.exists()
        data = json.loads(snap_file.read_text())
        assert "snap-skill" in data

        with _patch_db_path(db_path) as stack:
            stack.enter_context(patch("sagaflow.cli.Path.home", return_value=tmp_path))
            result = runner.invoke(
                main, ["portfolio", "regress", "--baseline", "test-baseline"]
            )
        assert result.exit_code == 0
        assert "no regressions" in result.output.lower()
