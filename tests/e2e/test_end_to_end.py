"""End-to-end smoke: run hello-world through the full stack.

Requires:
  - Temporal dev server on localhost:7233 (skip otherwise)
  - ANTHROPIC_API_KEY present OR ANTHROPIC_BASE_URL pointed at a working proxy (skip otherwise)

Run with: pytest tests/e2e/test_end_to_end.py -v
CI marks this file as skip-by-default via an env check.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skillflow.cli import main
from skillflow.inbox import Inbox


pytestmark = pytest.mark.skipif(
    not os.environ.get("SKILLFLOW_E2E"),
    reason="set SKILLFLOW_E2E=1 to run end-to-end tests",
)


def test_hello_world_end_to_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLFLOW_ROOT", str(tmp_path / "skillflow-root"))
    inbox_path = Path(tmp_path) / "skillflow-root" / "INBOX.md"

    runner = CliRunner()
    with patch("skillflow.durable.activities.notify_desktop"):
        result = runner.invoke(
            main, ["launch", "hello-world", "--name", "alice", "--await"], catch_exceptions=False
        )
    assert result.exit_code == 0, result.output

    inbox = Inbox(path=inbox_path)
    entries = inbox.unread()
    assert any(e.run_id.startswith("hello-world-") and e.status == "DONE" for e in entries)


def test_hello_world_output_contains_greeting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLFLOW_ROOT", str(tmp_path / "skillflow-root"))
    runner = CliRunner()
    with patch("skillflow.durable.activities.notify_desktop"):
        result = runner.invoke(
            main, ["launch", "hello-world", "--name", "bob", "--await"], catch_exceptions=False
        )
    assert result.exit_code == 0
    assert "bob" in result.output.lower() or "hello" in result.output.lower()


def test_worker_kill_mid_run_resumes(tmp_path, monkeypatch) -> None:
    """Launch → kill worker while running → restart worker → verify same result."""

    import subprocess
    import time

    monkeypatch.setenv("SKILLFLOW_ROOT", str(tmp_path / "skillflow-root"))

    # 1. Launch non-blocking
    runner = CliRunner()
    with patch("skillflow.durable.activities.notify_desktop"):
        result = runner.invoke(
            main, ["launch", "hello-world", "--name", "kitty"], catch_exceptions=False
        )
    assert result.exit_code == 0

    # 2. Find the auto-spawned worker PID via pgrep; kill it.
    pgrep = subprocess.run(
        ["pgrep", "-f", "skillflow.cli worker run --detached-child"],
        capture_output=True,
        text=True,
    )
    pids = [int(p) for p in pgrep.stdout.strip().split() if p]
    assert pids, "expected at least one auto-spawned worker"
    for pid in pids:
        os.kill(pid, 9)
    time.sleep(1)

    # 3. Run launch again (same skill, new run_id) — auto-spawn should revive a worker,
    #    and the original workflow (still in Temporal history) should resume and complete.
    with patch("skillflow.durable.activities.notify_desktop"):
        result2 = runner.invoke(
            main, ["launch", "hello-world", "--name", "spot", "--await"], catch_exceptions=False
        )
    assert result2.exit_code == 0

    # 4. Both runs should now show up in INBOX as DONE.
    inbox = Inbox(path=Path(tmp_path) / "skillflow-root" / "INBOX.md")
    # Wait up to 15s for the first run to complete after worker revival.
    deadline = time.time() + 15
    while time.time() < deadline:
        entries = inbox.unread()
        if sum(1 for e in entries if e.status == "DONE") >= 2:
            break
        time.sleep(0.5)
    entries = inbox.unread()
    assert sum(1 for e in entries if e.status == "DONE") >= 2
