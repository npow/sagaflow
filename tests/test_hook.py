import json
from datetime import datetime

from skillflow.hook import (
    HOOK_COMMAND,
    format_session_start_context,
    install,
    is_installed,
    uninstall,
)
from skillflow.inbox import InboxEntry, Inbox


def test_install_adds_hook_entry(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    install(settings_path=settings)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"  # unchanged
    assert any(
        h.get("command") == HOOK_COMMAND
        for event_list in data.get("hooks", {}).values()
        for h in event_list
    )


def test_install_is_idempotent(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    install(settings_path=settings)
    data = json.loads(settings.read_text())
    count = sum(
        1
        for event_list in data["hooks"].values()
        for h in event_list
        if h.get("command") == HOOK_COMMAND
    )
    assert count == 1


def test_install_creates_file_if_missing(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    assert settings.exists()
    data = json.loads(settings.read_text())
    assert is_installed(settings_path=settings)


def test_uninstall_removes_hook(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    uninstall(settings_path=settings)
    assert not is_installed(settings_path=settings)


def test_uninstall_noop_when_absent(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    uninstall(settings_path=settings)  # must not raise


def test_format_session_start_context_empty(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    assert format_session_start_context(inbox=inbox) == ""


def test_format_session_start_context_with_entries(tmp_path) -> None:
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
    ctx = format_session_start_context(inbox=inbox)
    assert "Unread skillflow runs" in ctx
    assert "r1" in ctx
    assert "DONE" in ctx
