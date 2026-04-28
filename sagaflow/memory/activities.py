"""Temporal activities for skill memory: commit_outcome and recall_outcomes."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from temporalio import activity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommitOutcomeInput:
    run_id: str
    skill: str
    terminal_label: str
    started_at: str
    completed_at: str
    duration_s: float
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    findings_json: str = "{}"
    findings_text: str = ""
    input_hash: str | None = None
    run_dir: str = ""
    primary_artifact: str | None = None
    sagaflow_version: str | None = None
    skill_commit: str | None = None


@activity.defn(name="commit_outcome")
async def commit_outcome(inp: CommitOutcomeInput) -> None:
    from sagaflow.memory.db import OutcomeRecord, SkillMemoryDB

    db = SkillMemoryDB.open()
    try:
        db.upsert_outcome(OutcomeRecord(
            run_id=inp.run_id,
            skill=inp.skill,
            terminal_label=inp.terminal_label,
            started_at=inp.started_at,
            completed_at=inp.completed_at,
            duration_s=inp.duration_s,
            cost_usd=inp.cost_usd,
            input_tokens=inp.input_tokens,
            output_tokens=inp.output_tokens,
            findings_json=inp.findings_json,
            findings_text=inp.findings_text,
            input_hash=inp.input_hash,
            run_dir=inp.run_dir,
            primary_artifact=inp.primary_artifact,
            sagaflow_version=inp.sagaflow_version,
            skill_commit=inp.skill_commit,
        ))
        logger.info("Committed outcome: %s (%s) → %s", inp.run_id, inp.skill, inp.terminal_label)
    finally:
        db.close()


@dataclass(frozen=True)
class RecallOutcomesInput:
    skill: str | None = None
    query: str | None = None
    limit: int = 10
    max_age_days: int = 90
    terminal_labels: tuple[str, ...] | None = None


@activity.defn(name="recall_outcomes")
async def recall_outcomes(inp: RecallOutcomesInput) -> list[dict]:
    from sagaflow.memory.db import SkillMemoryDB

    db = SkillMemoryDB.open()
    try:
        results = db.query_outcomes(
            skill=inp.skill,
            query=inp.query,
            limit=inp.limit,
            max_age_days=inp.max_age_days,
            terminal_labels=inp.terminal_labels,
        )
        logger.info(
            "Recalled %d outcomes (skill=%s, query=%s)",
            len(results), inp.skill, inp.query,
        )
        return [
            {
                "run_id": r.run_id,
                "skill": r.skill,
                "terminal_label": r.terminal_label,
                "completed_at": r.completed_at,
                "duration_s": r.duration_s,
                "cost_usd": r.cost_usd,
                "findings_json": r.findings_json,
                "findings_text": r.findings_text[:500],
                "input_hash": r.input_hash,
                "primary_artifact": r.primary_artifact,
            }
            for r in results
        ]
    finally:
        db.close()


def format_prior_outcomes(outcomes: list[dict]) -> str:
    """Render recall results as markdown for agent consumption."""
    if not outcomes:
        return ""
    import json

    lines = ["## Prior Outcomes (from skill memory)\n"]
    for o in outcomes:
        lines.append(f"### {o['run_id']} ({o.get('completed_at', '?')[:10]})")
        lines.append(f"**Label:** {o['terminal_label']}")
        dur = o.get("duration_s", 0)
        cost = o.get("cost_usd")
        cost_str = f"${cost:.2f}" if cost else "?"
        lines.append(f"**Duration:** {dur:.0f}s | **Cost:** {cost_str}")

        findings = o.get("findings_json", "{}")
        try:
            fj = json.loads(findings) if isinstance(findings, str) else findings
        except (json.JSONDecodeError, TypeError):
            fj = {}

        for k, v in fj.items():
            label = k.replace("_", " ").title()
            if isinstance(v, list):
                lines.append(f"**{label}:** {', '.join(str(x) for x in v[:5])}")
            elif isinstance(v, bool):
                lines.append(f"**{label}:** {'Yes' if v else 'No'}")
            else:
                lines.append(f"**{label}:** {v}")
        lines.append("")
    return "\n".join(lines)
