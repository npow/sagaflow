"""deep-qa-temporal skill registration."""

from __future__ import annotations

from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.registry import SkillRegistry, SkillSpec

from skills.deep_qa.activities import read_text_file
from skills.deep_qa.workflow import DeepQaInput, DeepQaWorkflow


def _build_input(
    *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
) -> DeepQaInput:
    path = cli_args.get("path") or ""
    if not path:
        raise ValueError("deep-qa requires --path <artifact>")
    artifact_type = str(cli_args.get("type", "doc"))
    try:
        max_rounds = int(cli_args.get("max_rounds", 3))
    except (TypeError, ValueError):
        max_rounds = 3
    return DeepQaInput(
        run_id=run_id,
        artifact_path=str(path),
        artifact_type=artifact_type,
        inbox_path=inbox_path,
        run_dir=run_dir,
        max_rounds=max_rounds,
        notify=True,
    )


def register(registry: SkillRegistry) -> None:
    registry.register(
        SkillSpec(
            name="deep-qa",
            workflow_cls=DeepQaWorkflow,
            activities=[write_artifact, emit_finding, spawn_subagent, read_text_file],
            build_input=_build_input,
        )
    )
