"""deep-debug-temporal skill registration."""

from __future__ import annotations

from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.registry import SkillRegistry, SkillSpec

from skills.deep_debug.workflow import DeepDebugInput, DeepDebugWorkflow


def _build_input(
    *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
) -> DeepDebugInput:
    symptom = str(cli_args.get("symptom", "")).strip()
    if not symptom:
        extra = cli_args.get("_extra")
        if isinstance(extra, list) and extra:
            symptom = " ".join(str(x) for x in extra)
    if not symptom:
        raise ValueError("deep-debug requires --arg symptom='...' or positional symptom text")
    repro = str(cli_args.get("reproduction", "")).strip()
    try:
        num = int(cli_args.get("num_hypotheses", 4))
    except (TypeError, ValueError):
        num = 4
    return DeepDebugInput(
        run_id=run_id,
        symptom=symptom,
        reproduction_command=repro,
        inbox_path=inbox_path,
        run_dir=run_dir,
        num_hypotheses=num,
        notify=True,
    )


def register(registry: SkillRegistry) -> None:
    registry.register(
        SkillSpec(
            name="deep-debug",
            workflow_cls=DeepDebugWorkflow,
            activities=[write_artifact, emit_finding, spawn_subagent],
            build_input=_build_input,
        )
    )
