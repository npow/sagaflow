from unittest.mock import patch

from skillflow.notify import notify_desktop, _macos_command, _linux_command


def test_macos_command_structure() -> None:
    cmd = _macos_command(title="T", body="B")
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    assert 'display notification "B" with title "T"' in cmd[2]


def test_linux_command_structure() -> None:
    cmd = _linux_command(title="T", body="B")
    assert cmd[0] == "notify-send"
    assert cmd[1] == "T"
    assert cmd[2] == "B"


def test_notify_desktop_macos_calls_osascript() -> None:
    with (
        patch("skillflow.notify._PLATFORM", "darwin"),
        patch("skillflow.notify.subprocess.run") as mock_run,
    ):
        notify_desktop(title="Hi", body="There")
    assert mock_run.call_args[0][0][0] == "osascript"


def test_notify_desktop_swallows_errors() -> None:
    with (
        patch("skillflow.notify._PLATFORM", "darwin"),
        patch("skillflow.notify.subprocess.run", side_effect=OSError("no binary")),
    ):
        notify_desktop(title="Hi", body="There")  # must not raise


def test_notify_desktop_unknown_platform_is_noop() -> None:
    with (
        patch("skillflow.notify._PLATFORM", "windows"),
        patch("skillflow.notify.subprocess.run") as mock_run,
    ):
        notify_desktop(title="Hi", body="There")
    mock_run.assert_not_called()
