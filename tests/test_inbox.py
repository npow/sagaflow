from datetime import datetime

import pytest

from skillflow.inbox import InboxEntry, Inbox


def test_append_and_unread(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    inbox.append(
        InboxEntry(
            run_id="hello-world-abc",
            skill="hello-world",
            status="DONE",
            summary="greeted alice",
            timestamp=datetime(2026, 4, 21, 14, 33, 22),
        )
    )
    unread = inbox.unread()
    assert len(unread) == 1
    assert unread[0].run_id == "hello-world-abc"
    assert unread[0].status == "DONE"


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
    assert len(inbox.unread()) == 1
    inbox.dismiss("r1")
    assert inbox.unread() == []


def test_dismiss_unknown_run_raises(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    with pytest.raises(KeyError, match="r1"):
        inbox.dismiss("r1")


def test_append_survives_empty_file(tmp_path) -> None:
    path = tmp_path / "INBOX.md"
    path.write_text("")
    inbox = Inbox(path=path)
    inbox.append(
        InboxEntry(
            run_id="r1",
            skill="s",
            status="FAILED",
            summary="err",
            timestamp=datetime(2026, 4, 21, 0, 0, 0),
        )
    )
    assert path.read_text().startswith("[2026-04-21 00:00:00] r1 FAILED s err  <unread>")


def test_multiple_entries_preserve_order(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    for i in range(3):
        inbox.append(
            InboxEntry(
                run_id=f"r{i}",
                skill="s",
                status="DONE",
                summary=f"ran {i}",
                timestamp=datetime(2026, 4, 21, i, 0, 0),
            )
        )
    unread = inbox.unread()
    assert [e.run_id for e in unread] == ["r0", "r1", "r2"]
