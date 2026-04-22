"""loop-until-done skill registration."""

from __future__ import annotations

from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.registry import SkillRegistry, SkillSpec

from skills.loop_until_done.workflow import LoopUntilDoneInput, LoopUntilDoneWorkflow


def _build_input(
    *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
) -> LoopUntilDoneInput:
    # --arg task="..." or positional text via _extra
    task = cli_args.get("task") or ""
    if not task:
        extra = cli_args.get("_extra", [])
        if isinstance(extra, list) and extra:
            task = " ".join(str(x) for x in extra)
    if not task:
        raise ValueError("loop-until-done requires --arg task='<description>' or positional text")
    try:
        max_iter = int(cli_args.get("max_iter", 5))
    except (TypeError, ValueError):
        max_iter = 5
    return LoopUntilDoneInput(
        run_id=run_id,
        task=str(task),
        inbox_path=inbox_path,
        run_dir=run_dir,
        max_iter=max_iter,
        notify=True,
    )


def register(registry: SkillRegistry) -> None:
    registry.register(
        SkillSpec(
            name="loop-until-done",
            workflow_cls=LoopUntilDoneWorkflow,
            activities=[write_artifact, emit_finding, spawn_subagent],
            build_input=_build_input,
        )
    )
