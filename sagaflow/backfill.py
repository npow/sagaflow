"""Backfill partial manifests for pre-existing runs.

Reconstructs ``run_manifest.json`` from INBOX.md entries and artifact scans.
Backfilled manifests are marked ``backfilled: true``; irrecoverable fields
(``steps``, ``input_hash``, ``workflow_content_hash``, token counts) are
written as ``null`` — never omitted.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from sagaflow.behavior import extract_signals
from sagaflow.manifest import RunManifest, _MANIFEST_FILE, _write_atomic

logger = logging.getLogger(__name__)

_INBOX_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+(?P<run_id>\S+)\s+(?P<status>\S+)\s+(?P<skill>\S+)\s+(?P<summary>.+?)(?:\s+<(?:unread|read)>)?$"
)


def _parse_inbox(inbox_path: Path) -> dict[str, dict[str, str]]:
    """Parse INBOX.md into {run_id: {ts, status, skill, summary}}."""
    entries: dict[str, dict[str, str]] = {}
    if not inbox_path.exists():
        return entries
    for line in inbox_path.read_text(encoding="utf-8").splitlines():
        m = _INBOX_LINE_RE.match(line.strip())
        if m:
            entries[m.group("run_id")] = {
                "ts": m.group("ts"),
                "status": m.group("status"),
                "skill": m.group("skill"),
                "summary": m.group("summary").strip(),
            }
    return entries


def backfill_run(run_dir: Path, inbox_entry: dict[str, str] | None = None) -> RunManifest:
    """Reconstruct a partial manifest from artifacts and optional INBOX entry."""
    run_id = run_dir.name
    skill = ""
    status = "UNKNOWN"
    summary = ""
    started_at = None

    if inbox_entry:
        skill = inbox_entry.get("skill", "")
        status = inbox_entry.get("status", "UNKNOWN")
        summary = inbox_entry.get("summary", "")
        ts_str = inbox_entry.get("ts", "")
        try:
            started_at = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").isoformat() + "Z"
        except (ValueError, TypeError):
            pass
    else:
        m = re.match(r"^([a-z][a-z0-9-]+)-\d{8}-\d{6}$", run_id)
        if m:
            skill = m.group(1)

    artifacts = []
    for f in sorted(run_dir.iterdir()):
        if f.name.startswith(".") or f.name == _MANIFEST_FILE:
            continue
        if f.is_file():
            artifacts.append({"name": f.name, "size_bytes": f.stat().st_size})

    signals = extract_signals(skill, run_dir)

    termination: dict[str, Any] = {"label": summary} if summary else {}

    manifest = RunManifest(
        run_id=run_id,
        skill=skill,
        status=_normalize_status(status),
        backfilled=True,
        timing={
            "started_at": started_at,
            "ended_at": None,
            "duration_seconds": None,
        },
        input={
            "args": None,
            "input_hash": None,
            "input_bytes": None,
        },
        skill_version={
            "git_sha": None,
            "git_dirty": None,
            "workflow_content_hash": None,
        },
        termination=termination,
        steps=[],
        cost={
            "total_input_tokens": None,
            "total_output_tokens": None,
            "estimated_cost_usd": None,
            "by_tier": {},
        },
        artifacts=artifacts,
        behavioral_signals={
            "type": signals.type,
            "source": signals.source,
            "raw": signals.raw,
        },
    )
    return manifest


def _normalize_status(inbox_status: str) -> str:
    s = inbox_status.upper()
    if s in ("DONE", "COMPLETED"):
        return "COMPLETED"
    if s in ("FAILED", "ERROR"):
        return "FAILED"
    if s in ("TRUNCATED", "EMPTY", "NO_CONSENSUS"):
        return "FAILED"
    return "COMPLETED"


def backfill_all(
    runs_dir: Path,
    inbox_path: Path,
    dry_run: bool = False,
    force: bool = False,
) -> list[str]:
    """Backfill manifests for all runs lacking one. Returns list of run_ids processed."""
    inbox_entries = _parse_inbox(inbox_path)
    processed: list[str] = []

    for rd in sorted(runs_dir.iterdir()):
        if not rd.is_dir():
            continue
        manifest_path = rd / _MANIFEST_FILE
        if manifest_path.exists() and not force:
            continue

        run_id = rd.name
        entry = inbox_entries.get(run_id)
        manifest = backfill_run(rd, entry)

        if dry_run:
            logger.info("would backfill %s (skill=%s)", run_id, manifest.skill)
        else:
            from dataclasses import asdict
            _write_atomic(manifest_path, asdict(manifest))
            logger.info("backfilled %s (skill=%s)", run_id, manifest.skill)
        processed.append(run_id)

    return processed
