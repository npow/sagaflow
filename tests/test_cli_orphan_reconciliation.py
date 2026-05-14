"""Tests for the cli list reconciliation and stale-runs probe logic.

These cover gaps that allowed two orphan-handling bugs to ship:

1. ``sagaflow list`` pulled status straight from Temporal and never
   reconciled against the on-disk manifest. The existing list test
   stubbed ``_list_workflows`` entirely so the bug couldn't surface.

2. ``_probe_stale_runs`` had zero tests. It required ``progress.json``
   before measuring freshness; skills that don't emit progress.json
   (fix-pr / autopilot) were silently invisible to the staleness check.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# _list_workflows reconciliation
# ---------------------------------------------------------------------------

def _fake_wf(wf_id: str, status: str) -> MagicMock:
    """Mimic the temporalio workflow execution row returned by list_workflows."""
    wf = MagicMock()
    wf.id = wf_id
    wf.status = MagicMock()
    wf.status.name = status
    return wf


def _write_manifest(run_dir: Path, status: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"status": status, "timing": {}})
    )


def test_list_workflows_prefers_terminal_manifest_over_temporal(monkeypatch, tmp_path):
    """When Temporal says RUNNING but manifest says TIMED_OUT, list shows TIMED_OUT.

    Regression test for the bug where a deep-research workflow that
    hit max_rounds (TIMED_OUT in manifest) sat under RUNNING in
    ``sagaflow list`` indefinitely because Temporal hadn't GC'd the
    history.
    """
    runs_dir = tmp_path / ".sagaflow" / "runs"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _write_manifest(runs_dir / "deep-research-A", "TIMED_OUT")
    _write_manifest(runs_dir / "fix-pr-B", "TERMINATED")
    _write_manifest(runs_dir / "deep-qa-C", "RUNNING")  # genuinely still running

    fake_workflows = [
        _fake_wf("sagaflow-deep-research-A", "RUNNING"),    # Temporal lags manifest
        _fake_wf("sagaflow-fix-pr-B", "RUNNING"),           # ditto
        _fake_wf("sagaflow-deep-qa-C", "RUNNING"),           # actually running
        _fake_wf("sagaflow-team-D", "RUNNING"),              # no manifest — trust Temporal
    ]

    async def _fake_list_workflows(*args, **kwargs):
        for wf in fake_workflows:
            yield wf

    fake_client = MagicMock()
    fake_client.list_workflows = _fake_list_workflows

    async def _fake_connect():
        return fake_client

    from sagaflow import cli

    with patch("sagaflow.temporal_client.connect", _fake_connect):
        rows = cli._list_workflows()

    by_id = {r["id"]: r["status"] for r in rows}
    assert by_id["sagaflow-deep-research-A"] == "TIMED_OUT", \
        "terminal manifest status must override Temporal's lagging RUNNING"
    assert by_id["sagaflow-fix-pr-B"] == "TERMINATED"
    assert by_id["sagaflow-deep-qa-C"] == "RUNNING", \
        "RUNNING manifest defers to Temporal (Temporal may be ahead while live)"
    assert by_id["sagaflow-team-D"] == "RUNNING", \
        "missing manifest falls back to Temporal status"


# ---------------------------------------------------------------------------
# _probe_stale_runs fallback
# ---------------------------------------------------------------------------

def test_probe_stale_runs_no_progress_json(monkeypatch, tmp_path):
    """RUNNING manifest without progress.json must still be checked for staleness.

    Regression for fix-pr-1314 case: the workflow doesn't emit
    progress.json, so the previous probe skipped it entirely and
    never reported it stale even after 3 hours of silence.
    """
    runs_dir = tmp_path / ".sagaflow" / "runs"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    stale_dir = runs_dir / "fix-pr-stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "run_manifest.json").write_text(json.dumps({"status": "RUNNING", "timing": {}}))
    (stale_dir / "phase2_iter4_prompt.md").write_text("some content")
    # Backdate everything 30 min so it crosses the 10-min staleness threshold
    old_ts = time.time() - 30 * 60
    for p in stale_dir.rglob("*"):
        os.utime(p, (old_ts, old_ts))

    from sagaflow import cli

    status, msg = cli._probe_stale_runs()
    assert status == "WARN"
    assert "fix-pr-stale" in (msg or "")


def test_probe_stale_runs_fresh_run_dir(monkeypatch, tmp_path):
    """A RUNNING manifest with recent file writes must not be flagged stale."""
    runs_dir = tmp_path / ".sagaflow" / "runs"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    live_dir = runs_dir / "fix-pr-live"
    live_dir.mkdir(parents=True)
    (live_dir / "run_manifest.json").write_text(json.dumps({"status": "RUNNING", "timing": {}}))
    (live_dir / "phase2_iter0_prompt.md").write_text("fresh")

    from sagaflow import cli

    status, _msg = cli._probe_stale_runs()
    assert status == "OK"


def test_probe_stale_runs_terminal_manifest_skipped(monkeypatch, tmp_path):
    """Terminal manifests are not subject to staleness checks even if old."""
    runs_dir = tmp_path / ".sagaflow" / "runs"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    done_dir = runs_dir / "deep-research-old"
    done_dir.mkdir(parents=True)
    (done_dir / "run_manifest.json").write_text(json.dumps({"status": "COMPLETED", "timing": {}}))
    (done_dir / "report.md").write_text("old")
    old_ts = time.time() - 24 * 60 * 60
    for p in done_dir.rglob("*"):
        os.utime(p, (old_ts, old_ts))

    from sagaflow import cli

    status, _msg = cli._probe_stale_runs()
    assert status == "OK"
