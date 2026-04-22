from unittest.mock import patch

from click.testing import CliRunner

from skillflow.cli import main


def test_hook_install_calls_installer() -> None:
    runner = CliRunner()
    with patch("skillflow.hook.install") as mock_install:
        result = runner.invoke(main, ["hook", "install"])
    assert result.exit_code == 0
    mock_install.assert_called_once()


def test_hook_uninstall_calls_uninstaller() -> None:
    runner = CliRunner()
    with patch("skillflow.hook.uninstall") as mock_uninstall:
        result = runner.invoke(main, ["hook", "uninstall"])
    assert result.exit_code == 0
    mock_uninstall.assert_called_once()


def test_hook_session_start_emits_context(tmp_path) -> None:
    from datetime import datetime
    from skillflow.inbox import Inbox, InboxEntry

    inbox_path = tmp_path / "INBOX.md"
    inbox = Inbox(path=inbox_path)
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
    with patch("skillflow.cli._inbox", return_value=inbox):
        result = runner.invoke(main, ["hook", "session-start"])
    assert result.exit_code == 0
    assert "r1" in result.output
