"""Tests for sagaflow.memory — SQLite + FTS5 skill memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from sagaflow.memory.db import OutcomeRecord, PatternRecord, SkillMemoryDB
from sagaflow.memory.activities import format_prior_outcomes


@pytest.fixture
def db(tmp_path: Path) -> SkillMemoryDB:
    d = SkillMemoryDB.open(tmp_path / "test-memory.db")
    yield d
    d.close()


def _make_outcome(**overrides) -> OutcomeRecord:
    defaults = dict(
        run_id="run-001",
        skill="deep-qa",
        terminal_label="qa_complete",
        started_at="2026-04-28T10:00:00Z",
        completed_at="2026-04-28T10:05:00Z",
        duration_s=300.0,
        cost_usd=0.42,
        input_tokens=5000,
        output_tokens=3000,
        findings_json='{"defects_found": 5}',
        findings_text="Found 5 critical defects in authentication module",
        input_hash="sha256:abc123",
        run_dir="/root/.sagaflow/runs/run-001",
        primary_artifact="qa-report.md",
        sagaflow_version="0.9.0",
        skill_commit="abc1234",
    )
    defaults.update(overrides)
    return OutcomeRecord(**defaults)


class TestSkillMemoryDB:
    def test_open_creates_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "new.db"
        d = SkillMemoryDB.open(db_path)
        assert db_path.exists()
        d.close()

    def test_wal_mode(self, db: SkillMemoryDB) -> None:
        row = db._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_tables_exist(self, db: SkillMemoryDB) -> None:
        tables = {
            r[0]
            for r in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "outcomes" in tables
        assert "patterns" in tables


class TestUpsertOutcome:
    def test_insert(self, db: SkillMemoryDB) -> None:
        rec = _make_outcome()
        db.upsert_outcome(rec)
        assert db.count_outcomes() == 1

    def test_upsert_replaces(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(terminal_label="first"))
        db.upsert_outcome(_make_outcome(terminal_label="second"))
        assert db.count_outcomes() == 1
        got = db.get_outcome("run-001")
        assert got is not None
        assert got.terminal_label == "second"

    def test_all_fields_roundtrip(self, db: SkillMemoryDB) -> None:
        rec = _make_outcome()
        db.upsert_outcome(rec)
        got = db.get_outcome("run-001")
        assert got is not None
        assert got.run_id == rec.run_id
        assert got.skill == rec.skill
        assert got.terminal_label == rec.terminal_label
        assert got.duration_s == rec.duration_s
        assert got.cost_usd == rec.cost_usd
        assert got.input_tokens == rec.input_tokens
        assert got.output_tokens == rec.output_tokens
        assert got.findings_json == rec.findings_json
        assert got.findings_text == rec.findings_text
        assert got.input_hash == rec.input_hash
        assert got.primary_artifact == rec.primary_artifact
        assert got.sagaflow_version == rec.sagaflow_version
        assert got.skill_commit == rec.skill_commit

    def test_nullable_fields(self, db: SkillMemoryDB) -> None:
        rec = _make_outcome(cost_usd=None, input_tokens=None, output_tokens=None)
        db.upsert_outcome(rec)
        got = db.get_outcome("run-001")
        assert got is not None
        assert got.cost_usd is None
        assert got.input_tokens is None


class TestQueryOutcomes:
    def test_filter_by_skill(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(run_id="r1", skill="deep-qa"))
        db.upsert_outcome(_make_outcome(run_id="r2", skill="deep-design"))
        db.upsert_outcome(_make_outcome(run_id="r3", skill="deep-qa"))
        results = db.query_outcomes(skill="deep-qa")
        assert len(results) == 2
        assert all(r.skill == "deep-qa" for r in results)

    def test_filter_by_terminal_labels(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(run_id="r1", terminal_label="qa_complete"))
        db.upsert_outcome(_make_outcome(run_id="r2", terminal_label="FAILED"))
        db.upsert_outcome(_make_outcome(run_id="r3", terminal_label="qa_complete"))
        results = db.query_outcomes(terminal_labels=("qa_complete",))
        assert len(results) == 2

    def test_limit(self, db: SkillMemoryDB) -> None:
        for i in range(10):
            db.upsert_outcome(_make_outcome(run_id=f"r{i}"))
        results = db.query_outcomes(limit=3)
        assert len(results) == 3

    def test_ordered_by_completed_at_desc(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(run_id="old", completed_at="2026-04-01T00:00:00Z"))
        db.upsert_outcome(_make_outcome(run_id="new", completed_at="2026-04-28T00:00:00Z"))
        results = db.query_outcomes()
        assert results[0].run_id == "new"

    def test_empty_result(self, db: SkillMemoryDB) -> None:
        results = db.query_outcomes(skill="nonexistent")
        assert results == []


class TestFTSQuery:
    def test_basic_fts(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(
            run_id="r1",
            findings_text="authentication bypass vulnerability in login handler",
        ))
        db.upsert_outcome(_make_outcome(
            run_id="r2",
            findings_text="database connection pool exhaustion under load",
        ))
        results = db.query_outcomes(query="authentication")
        assert len(results) == 1
        assert results[0].run_id == "r1"

    def test_fts_with_skill_filter(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(
            run_id="r1", skill="deep-qa",
            findings_text="memory leak in worker process",
        ))
        db.upsert_outcome(_make_outcome(
            run_id="r2", skill="deep-debug",
            findings_text="memory leak in scheduler",
        ))
        results = db.query_outcomes(query="memory leak", skill="deep-qa")
        assert len(results) == 1
        assert results[0].run_id == "r1"

    def test_fts_no_match(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(findings_text="nothing relevant here"))
        results = db.query_outcomes(query="xyznonexistent")
        assert results == []


class TestListAndCount:
    def test_list_all(self, db: SkillMemoryDB) -> None:
        for i in range(5):
            db.upsert_outcome(_make_outcome(run_id=f"r{i}"))
        assert len(db.list_outcomes()) == 5
        assert db.count_outcomes() == 5

    def test_list_by_skill(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(run_id="r1", skill="deep-qa"))
        db.upsert_outcome(_make_outcome(run_id="r2", skill="deep-design"))
        assert len(db.list_outcomes(skill="deep-qa")) == 1
        assert db.count_outcomes(skill="deep-qa") == 1

    def test_list_limit(self, db: SkillMemoryDB) -> None:
        for i in range(10):
            db.upsert_outcome(_make_outcome(run_id=f"r{i}"))
        assert len(db.list_outcomes(limit=3)) == 3

    def test_get_missing(self, db: SkillMemoryDB) -> None:
        assert db.get_outcome("nonexistent") is None


class TestPatterns:
    def test_upsert_new(self, db: SkillMemoryDB) -> None:
        pat = PatternRecord(
            skill="deep-qa",
            pattern_type="failure_mode",
            pattern_key="timeout_on_large_input",
            description="Timeouts when input exceeds 50KB",
            first_seen_run="r1",
            last_seen_run="r1",
        )
        db.upsert_pattern(pat)
        results = db.query_patterns(skill="deep-qa")
        assert len(results) == 1
        assert results[0].pattern_key == "timeout_on_large_input"

    def test_upsert_increments_frequency(self, db: SkillMemoryDB) -> None:
        pat = PatternRecord(
            skill="deep-qa",
            pattern_type="failure_mode",
            pattern_key="timeout",
            description="First occurrence",
            first_seen_run="r1",
            last_seen_run="r1",
        )
        db.upsert_pattern(pat)
        pat2 = PatternRecord(
            skill="deep-qa",
            pattern_type="failure_mode",
            pattern_key="timeout",
            description="Updated description",
            first_seen_run="r1",
            last_seen_run="r2",
        )
        db.upsert_pattern(pat2)
        results = db.query_patterns(skill="deep-qa")
        assert len(results) == 1
        assert results[0].frequency == 2

    def test_min_frequency_filter(self, db: SkillMemoryDB) -> None:
        for i in range(3):
            db.upsert_pattern(PatternRecord(
                skill="deep-qa",
                pattern_type="failure_mode",
                pattern_key="timeout",
                description="keeps happening",
                first_seen_run="r1",
                last_seen_run=f"r{i}",
            ))
        db.upsert_pattern(PatternRecord(
            skill="deep-qa",
            pattern_type="failure_mode",
            pattern_key="rare_thing",
            description="one-off",
            first_seen_run="r5",
            last_seen_run="r5",
        ))
        high_freq = db.query_patterns(min_frequency=2)
        assert len(high_freq) == 1
        assert high_freq[0].pattern_key == "timeout"


class TestDeleteExpired:
    def test_deletes_expired(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(run_id="r1"))
        db._conn.execute(
            "UPDATE outcomes SET expires_at = datetime('now', '-1 day') WHERE run_id = 'r1'"
        )
        db._conn.commit()
        deleted = db.delete_expired()
        assert deleted == 1
        assert db.count_outcomes() == 0

    def test_keeps_non_expired(self, db: SkillMemoryDB) -> None:
        db.upsert_outcome(_make_outcome(run_id="r1"))
        db._conn.execute(
            "UPDATE outcomes SET expires_at = datetime('now', '+30 day') WHERE run_id = 'r1'"
        )
        db._conn.commit()
        deleted = db.delete_expired()
        assert deleted == 0
        assert db.count_outcomes() == 1


class TestFormatPriorOutcomes:
    def test_empty(self) -> None:
        assert format_prior_outcomes([]) == ""

    def test_renders_markdown(self) -> None:
        outcomes = [
            {
                "run_id": "run-001",
                "completed_at": "2026-04-28T10:05:00Z",
                "terminal_label": "qa_complete",
                "duration_s": 300.0,
                "cost_usd": 0.42,
                "findings_json": '{"defects_found": 5, "categories": ["auth", "input"]}',
            }
        ]
        text = format_prior_outcomes(outcomes)
        assert "## Prior Outcomes" in text
        assert "run-001" in text
        assert "qa_complete" in text
        assert "$0.42" in text
        assert "Defects Found" in text
        assert "auth, input" in text

    def test_handles_bad_json(self) -> None:
        outcomes = [
            {
                "run_id": "run-bad",
                "completed_at": "2026-04-28",
                "terminal_label": "FAILED",
                "duration_s": 10.0,
                "cost_usd": None,
                "findings_json": "not json at all {{{",
            }
        ]
        text = format_prior_outcomes(outcomes)
        assert "run-bad" in text
        assert "?" in text  # cost_usd is None → "?"

    def test_handles_dict_findings(self) -> None:
        outcomes = [
            {
                "run_id": "run-dict",
                "completed_at": "2026-04-28",
                "terminal_label": "done",
                "duration_s": 60.0,
                "findings_json": {"already": "parsed"},
            }
        ]
        text = format_prior_outcomes(outcomes)
        assert "Already" in text
        assert "parsed" in text
