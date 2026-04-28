"""Cassette: ordered record of activity inputs and outputs for deterministic replay.

A cassette captures every ``spawn_subagent`` call during a workflow run so the
same workflow can be re-executed without making real LLM calls.  Cassettes are
saved to ``{run_dir}/.replay_cassette.json`` automatically during normal runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CASSETTE_FILE = ".replay_cassette.json"
_CASSETTE_VERSION = 1


@dataclass
class CassetteEntry:
    seq: int
    activity: str
    role: str
    tier: str
    input_hash: str
    output: dict[str, Any]
    duration_seconds: float


@dataclass
class Cassette:
    version: int = _CASSETTE_VERSION
    run_id: str = ""
    skill: str = ""
    recorded_at: str = ""
    entries: list[CassetteEntry] = field(default_factory=list)


def cassette_path(run_dir: Path) -> Path:
    return run_dir / _CASSETTE_FILE


def load(run_dir: Path) -> Cassette:
    p = cassette_path(run_dir)
    if not p.exists():
        raise FileNotFoundError(f"No cassette at {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return Cassette(
        version=data.get("version", 1),
        run_id=data.get("run_id", ""),
        skill=data.get("skill", ""),
        recorded_at=data.get("recorded_at", ""),
        entries=[CassetteEntry(**e) for e in data.get("entries", [])],
    )


def save(cassette: Cassette, run_dir: Path) -> Path:
    p = cassette_path(run_dir)
    data = {
        "version": cassette.version,
        "run_id": cassette.run_id,
        "skill": cassette.skill,
        "recorded_at": cassette.recorded_at,
        "entries": [asdict(e) for e in cassette.entries],
    }
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return p


def hash_input(role: str, system_prompt: str, user_prompt: str) -> str:
    content = f"{role}:{system_prompt[:500]}:{user_prompt[:2000]}"
    return f"sha256:{hashlib.sha256(content.encode()).hexdigest()[:16]}"


def record_entry(
    run_dir: Path,
    run_id: str,
    skill: str,
    activity_name: str,
    role: str,
    tier: str,
    input_hash: str,
    output: dict[str, Any],
    duration_seconds: float,
) -> None:
    """Append one entry to the cassette file. Single-writer (Temporal activity)."""
    p = cassette_path(run_dir)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = {
            "version": _CASSETTE_VERSION,
            "run_id": run_id,
            "skill": skill,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "entries": [],
        }

    seq = len(data["entries"])
    data["entries"].append({
        "seq": seq,
        "activity": activity_name,
        "role": role,
        "tier": tier,
        "input_hash": input_hash,
        "output": output,
        "duration_seconds": duration_seconds,
    })

    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def list_cassettes(runs_dir: Path) -> list[dict[str, Any]]:
    """List all runs that have cassettes, newest first."""
    results: list[dict[str, Any]] = []
    if not runs_dir.is_dir():
        return results
    for d in sorted(runs_dir.iterdir(), reverse=True):
        cp = cassette_path(d)
        if cp.exists():
            try:
                c = load(d)
                results.append({
                    "run_id": c.run_id or d.name,
                    "skill": c.skill,
                    "entries": len(c.entries),
                    "recorded_at": c.recorded_at,
                })
            except Exception:
                results.append({"run_id": d.name, "skill": "?", "entries": 0, "recorded_at": "?"})
    return results
