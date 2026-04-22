"""deep-design-temporal skill registration."""

from __future__ import annotations

from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.registry import SkillRegistry, SkillSpec

from skills.deep_design.workflow import DeepDesignInput, DeepDesignWorkflow


def _build_input(
    *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
) -> DeepDesignInput:
    concept = cli_args.get("concept") or ""
    if not concept:
        # Fall back to positional extras joined as a single string.
        extras = cli_args.get("_extra", [])
        if isinstance(extras, list):
            concept = " ".join(str(e) for e in extras)
    if not concept:
        raise ValueError("deep-design requires --arg concept='<description>'")
    try:
        max_rounds = int(cli_args.get("max_rounds", 2))
    except (TypeError, ValueError):
        max_rounds = 2
    return DeepDesignInput(
        run_id=run_id,
        concept=str(concept),
        inbox_path=inbox_path,
        run_dir=run_dir,
        max_rounds=max_rounds,
        notify=True,
    )


def register(registry: SkillRegistry) -> None:
    registry.register(
        SkillSpec(
            name="deep-design",
            workflow_cls=DeepDesignWorkflow,
            activities=[write_artifact, emit_finding, spawn_subagent],
            build_input=_build_input,
        )
    )
