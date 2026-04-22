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
