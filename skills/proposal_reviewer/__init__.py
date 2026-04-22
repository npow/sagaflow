"""proposal-reviewer skill registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.registry import SkillRegistry, SkillSpec

from skills.proposal_reviewer.workflow import ProposalReviewInput, ProposalReviewWorkflow

_MIN_WORDS = 200


def _build_input(
    *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
) -> ProposalReviewInput:
    proposal_text = str(cli_args.get("proposal") or "")
    path = cli_args.get("path") or ""

    if not proposal_text and path:
        p = Path(str(path)).expanduser().resolve()
        proposal_text = p.read_text(encoding="utf-8", errors="replace")

    if not proposal_text:
        raise ValueError(
            "proposal-reviewer requires --arg proposal='...' or --path <file>"
        )

    word_count = len(proposal_text.split())
    if word_count < _MIN_WORDS:
        raise ValueError(
            f"Proposal too short: {word_count} words (minimum {_MIN_WORDS})."
        )

    notify = bool(cli_args.get("notify", True))
    return ProposalReviewInput(
        run_id=run_id,
        proposal_text=proposal_text,
        inbox_path=inbox_path,
        run_dir=run_dir,
        notify=notify,
    )


def register(registry: SkillRegistry) -> None:
    registry.register(
        SkillSpec(
            name="proposal-reviewer",
            workflow_cls=ProposalReviewWorkflow,
            activities=[write_artifact, emit_finding, spawn_subagent],
            build_input=_build_input,
        )
    )
