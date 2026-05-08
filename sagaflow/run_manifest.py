"""Run manifest: progressive metadata capture for behavior versioning.

Each sagaflow run produces a ``run_manifest.json`` in its run directory.
The manifest is written at three points:

1. **initialize** — at launch, before any steps execute.
2. **append_step** — after each subagent call completes.
3. **finalize** — at termination (success or failure).

All writes use ``filelock.FileLock`` across the full read-mutate-write cycle.
Lock timeout is 2 seconds; on timeout the write is skipped (run continues).
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from sagaflow.cost import estimate_cost

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT = 2.0
_MANIFEST_FILE = "run_manifest.json"
_LOCK_FILE = ".manifest.lock"
_SCHEMA_VERSION = 1
_FINALIZE_RETRIES = 3


@dataclass
class StepRecord:
    step: int
    role: str
    model: str
    tier: str
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    status: str
    output_schema_used: bool = False


@dataclass
class RunManifest:
    schema_version: int = _SCHEMA_VERSION
    run_id: str = ""
    skill: str = ""
    status: str = "RUNNING"
    backfilled: bool = False

    timing: dict[str, Any] = field(default_factory=dict)
    input: dict[str, Any] = field(default_factory=dict)
    skill_version: dict[str, Any] = field(default_factory=dict)
    termination: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    behavioral_signals: dict[str, Any] = field(default_factory=dict)


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / _MANIFEST_FILE


def _lock_path(run_dir: Path) -> Path:
    return run_dir / _LOCK_FILE


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".manifest_"
    )
    try:
        with open(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        Path(tmp_path).replace(path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _read_manifest(run_dir: Path) -> dict[str, Any]:
    mp = _manifest_path(run_dir)
    if not mp.exists():
        return asdict(RunManifest())
    data: dict[str, Any] = json.loads(mp.read_text(encoding="utf-8"))
    return data


def read_manifest(run_id: str) -> RunManifest:
    """Load a manifest by run_id. Returns a RunManifest dataclass."""
    from sagaflow.paths import Paths
    run_dir = Paths.from_env().run_dir_for(run_id)
    mp = _manifest_path(run_dir)
    if not mp.exists():
        raise FileNotFoundError(f"no manifest for run {run_id} at {mp}")
    data = json.loads(mp.read_text(encoding="utf-8"))
    return _dict_to_manifest(data)


def _dict_to_manifest(data: dict[str, Any]) -> RunManifest:
    m = RunManifest()
    for k, v in data.items():
        if hasattr(m, k):
            setattr(m, k, v)
    return m


def _compute_file_hash(path: Path) -> str | None:
    resolved = path.resolve()
    if not resolved.exists():
        return None
    content = resolved.read_bytes()
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _git_info(workflow_path: Path | None) -> dict[str, Any]:
    """Best-effort git metadata from the workflow file's repo."""
    import subprocess

    info: dict[str, Any] = {"git_sha": None, "git_dirty": None, "workflow_content_hash": None}
    if workflow_path and workflow_path.exists():
        info["workflow_content_hash"] = _compute_file_hash(workflow_path)

    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if sha.returncode == 0:
            info["git_sha"] = sha.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if dirty.returncode == 0:
            info["git_dirty"] = bool(dirty.stdout.strip())
    except Exception:
        pass
    return info


def initialize_manifest(
    run_dir: Path,
    run_id: str,
    skill: str,
    args: dict[str, Any],
    input_path: str | None = None,
    workflow_path: Path | None = None,
) -> None:
    """Write the initial manifest at launch time."""
    input_hash = None
    input_bytes = None
    if input_path:
        p = Path(input_path).resolve()
        if p.exists():
            input_hash = _compute_file_hash(p)
            input_bytes = p.stat().st_size

    manifest_data = asdict(RunManifest(
        run_id=run_id,
        skill=skill,
        status="RUNNING",
        timing={
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": None,
            "duration_seconds": None,
        },
        input={
            "args": args,
            "input_hash": input_hash,
            "input_bytes": input_bytes,
        },
        skill_version=_git_info(workflow_path),
    ))

    lock = _lock_path(run_dir)
    try:
        with FileLock(lock, timeout=_LOCK_TIMEOUT):
            _write_atomic(_manifest_path(run_dir), manifest_data)
    except Timeout:
        logger.warning("manifest lock timeout during initialize for %s", run_dir)


def append_step(run_dir: Path, step: StepRecord) -> None:
    """Append a step record to the manifest. Lock-protected RMW."""
    lock = _lock_path(run_dir)
    try:
        with FileLock(lock, timeout=_LOCK_TIMEOUT):
            data = _read_manifest(run_dir)
            steps = data.setdefault("steps", [])
            steps.append(asdict(step))

            cost = data.setdefault("cost", {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "estimated_cost_usd": 0.0,
                "by_tier": {},
            })
            cost["total_input_tokens"] = cost.get("total_input_tokens", 0) + step.input_tokens
            cost["total_output_tokens"] = cost.get("total_output_tokens", 0) + step.output_tokens
            step_cost = estimate_cost(step.tier, step.input_tokens, step.output_tokens)
            cost["estimated_cost_usd"] = round(cost.get("estimated_cost_usd", 0.0) + step_cost, 6)

            tier_entry = cost.setdefault("by_tier", {}).setdefault(step.tier, {
                "input": 0, "output": 0, "cost_usd": 0.0,
            })
            tier_entry["input"] = tier_entry.get("input", 0) + step.input_tokens
            tier_entry["output"] = tier_entry.get("output", 0) + step.output_tokens
            tier_entry["cost_usd"] = round(tier_entry.get("cost_usd", 0.0) + step_cost, 6)

            _write_atomic(_manifest_path(run_dir), data)
    except Timeout:
        logger.warning(
            "manifest lock timeout for %s — step %d skipped", run_dir, step.step
        )


def finalize_manifest(
    run_dir: Path,
    status: str,
    termination: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Write terminal status and behavioral signals. Idempotent — no-op if already terminal.

    Retries up to 3 times with exponential backoff on transient errors.
    """
    for attempt in range(_FINALIZE_RETRIES):
        try:
            _finalize_once(run_dir, status, termination, error)
            return
        except Timeout:
            delay = 2 ** attempt
            logger.warning(
                "manifest finalize lock timeout (attempt %d/%d) for %s — retrying in %ds",
                attempt + 1, _FINALIZE_RETRIES, run_dir, delay,
            )
            time.sleep(delay)
        except OSError as exc:
            delay = 2 ** attempt
            logger.warning(
                "manifest finalize I/O error (attempt %d/%d) for %s: %s — retrying in %ds",
                attempt + 1, _FINALIZE_RETRIES, run_dir, exc, delay,
            )
            time.sleep(delay)
    logger.error("manifest finalize failed after %d attempts for %s", _FINALIZE_RETRIES, run_dir)


def _finalize_once(
    run_dir: Path,
    status: str,
    termination: dict[str, Any] | None,
    error: str | None,
) -> None:
    from sagaflow.behavior import extract_signals

    lock = _lock_path(run_dir)
    with FileLock(lock, timeout=_LOCK_TIMEOUT):
        data = _read_manifest(run_dir)

        if data.get("status") in ("COMPLETED", "FAILED"):
            return

        data["status"] = status
        if termination:
            data["termination"] = termination
        if error:
            data.setdefault("termination", {})["error"] = error

        started = data.get("timing", {}).get("started_at")
        now = datetime.now(timezone.utc)
        data.setdefault("timing", {})["ended_at"] = now.isoformat()
        if started:
            try:
                start_dt = datetime.fromisoformat(started)
                data["timing"]["duration_seconds"] = round(
                    (now - start_dt).total_seconds(), 1
                )
            except (ValueError, TypeError):
                pass

        artifacts = []
        for f in sorted(run_dir.iterdir()):
            if f.name.startswith(".") or f.name == _MANIFEST_FILE:
                continue
            if f.is_file():
                artifacts.append({"name": f.name, "size_bytes": f.stat().st_size})
        data["artifacts"] = artifacts

        skill = data.get("skill", "")
        signals = extract_signals(skill, run_dir)
        data["behavioral_signals"] = {
            "type": signals.type,
            "source": signals.source,
            "raw": signals.raw,
        }

        _write_atomic(_manifest_path(run_dir), data)


def cleanup_stale_runs(max_age_hours: float = 1.0) -> int:
    """Mark RUNNING runs whose manifest hasn't been modified recently as TIMED_OUT.

    Called at worker startup to clean up zombies from prior crashes.
    Emits inbox entries and desktop notifications for each cleaned run so
    external monitors (agent sessions, users) learn about the death.
    Returns the number of runs cleaned up.
    """
    from sagaflow.inbox import Inbox, InboxEntry
    from sagaflow.notify import notify_desktop
    from sagaflow.paths import Paths

    paths = Paths.from_env()
    runs_dir = paths.runs_dir
    if not runs_dir.exists():
        return 0

    import os

    inbox = Inbox(path=paths.inbox)
    cutoff = time.time() - max_age_hours * 3600
    cleaned = 0
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        manifest_file = _manifest_path(run_dir)
        if not manifest_file.exists():
            continue
        if os.path.getmtime(manifest_file) > cutoff:
            continue
        try:
            data = _read_manifest(run_dir)
        except Exception:
            continue
        if data.get("status") != "RUNNING":
            continue
        try:
            finalize_manifest(
                run_dir,
                status="TIMED_OUT",
                termination={"reason": "stale_cleanup: worker restarted, run was still RUNNING"},
            )
            cleaned += 1
            run_id = run_dir.name
            skill = data.get("skill", "unknown")
            summary = f"stale_cleanup: run was still RUNNING when worker restarted"
            logger.info("cleaned up stale run %s (skill=%s)", run_id, skill)
            try:
                inbox.append(InboxEntry(
                    run_id=run_id,
                    skill=skill,
                    status="TIMED_OUT",
                    summary=summary,
                    timestamp=datetime.now(timezone.utc),
                ))
                notify_desktop(
                    title=f"sagaflow: {run_id} TIMED_OUT",
                    body=f"{skill}: {summary}",
                )
            except Exception as notify_exc:
                logger.warning("failed to notify for stale run %s: %s", run_id, notify_exc)
        except Exception as exc:
            logger.warning("failed to clean up stale run %s: %s", run_dir.name, exc)
    return cleaned


def write_budget_result(
    run_dir: Path,
    accumulated_cost_usd: float,
    max_cost_usd: float | None,
    step_count: int,
    alerts_fired: list[float],
    final_decision: str,
) -> None:
    """Persist budget enforcement state into the manifest."""
    lock = _lock_path(run_dir)
    try:
        with FileLock(lock, timeout=_LOCK_TIMEOUT):
            data = _read_manifest(run_dir)
            data["budget_result"] = {
                "final_cost_usd": round(accumulated_cost_usd, 6),
                "max_cost_usd": max_cost_usd,
                "step_count": step_count,
                "alerts_fired": sorted(alerts_fired),
                "final_decision": final_decision,
            }
            _write_atomic(_manifest_path(run_dir), data)
    except Timeout:
        logger.warning("manifest lock timeout writing budget_result for %s", run_dir)
