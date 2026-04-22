from datetime import datetime
from unittest.mock import patch

from click.testing import CliRunner

from sagaflow.cli import main
from sagaflow.inbox import Inbox, InboxEntry


def test_inbox_prints_entries(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    inbox.append(
        InboxEntry(
            run_id="r1",
            skill="hello-world",
            status="DONE",
            summary="hi",
            timestamp=datetime(2026, 4, 21, 14, 0, 0),
        )
    )
    runner = CliRunner()
    with patch("sagaflow.cli._inbox", return_value=inbox):
        result = runner.invoke(main, ["inbox"])
    assert result.exit_code == 0
    assert "r1" in result.output
    assert "DONE" in result.output


def test_inbox_empty_message(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    runner = CliRunner()
    with patch("sagaflow.cli._inbox", return_value=inbox):
        result = runner.invoke(main, ["inbox"])
    assert result.exit_code == 0
    assert "no unread entries" in result.output.lower()


def test_dismiss_marks_read(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    inbox.append(
        InboxEntry(
            run_id="r1",
            skill="s",
            status="DONE",
            summary="",
            timestamp=datetime(2026, 4, 21, 0, 0, 0),
        )
    )
    runner = CliRunner()
    with patch("sagaflow.cli._inbox", return_value=inbox):
        result = runner.invoke(main, ["dismiss", "r1"])
    assert result.exit_code == 0
    assert inbox.unread() == []


def test_list_prints_running(tmp_path) -> None:
    runner = CliRunner()
    with patch(
        "sagaflow.cli._list_workflows",
        return_value=[{"id": "wf-1", "status": "RUNNING"}],
    ):
        result = runner.invoke(main, ["list"])
    assert result.exit_code == 0
    assert "wf-1" in result.output
    assert "RUNNING" in result.output
