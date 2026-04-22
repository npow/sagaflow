"""Append-only inbox for terminal workflow transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

UNREAD_TAG = "<unread>"
DISMISSED_TAG = "<dismissed>"

_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+"
    r"(?P<run_id>\S+)\s+"
    r"(?P<status>\S+)\s+"
    r"(?P<skill>\S+)\s+"
    r"(?P<summary>.*?)\s+"
    r"(?P<tag><unread>|<dismissed>)\s*$"
)


@dataclass(frozen=True)
class InboxEntry:
    run_id: str
    skill: str
    status: str
    summary: str
    timestamp: datetime

    def format(self, tag: str) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        summary = self.summary or "-"
        return f"[{ts}] {self.run_id} {self.status} {self.skill} {summary}  {tag}\n"


class Inbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, entry: InboxEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.format(UNREAD_TAG))

    def unread(self) -> list[InboxEntry]:
        return [entry for entry, tag in self._iter() if tag == UNREAD_TAG]

    def dismiss(self, run_id: str) -> None:
        lines: list[str] = []
        found = False
        for entry, tag in self._iter():
            if entry.run_id == run_id and tag == UNREAD_TAG:
                lines.append(entry.format(DISMISSED_TAG))
                found = True
            else:
                lines.append(entry.format(tag))
        if not found:
            raise KeyError(f"no unread entry for run_id={run_id}")
        self.path.write_text("".join(lines), encoding="utf-8")

    def _iter(self) -> list[tuple[InboxEntry, str]]:
        if not self.path.exists():
            return []
        out: list[tuple[InboxEntry, str]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            m = _LINE_RE.match(line)
            if not m:
                continue
            ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S")
            summary = "" if m["summary"] == "-" else m["summary"]
            out.append(
                (
                    InboxEntry(
                        run_id=m["run_id"],
                        skill=m["skill"],
                        status=m["status"],
                        summary=summary,
                        timestamp=ts,
                    ),
                    m["tag"],
                )
            )
        return out
