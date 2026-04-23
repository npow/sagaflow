"""flaky-test-diagnoser skill registration."""

from __future__ import annotations

from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.prompts import PromptNotFoundError, load_claude_skill_prompt
from sagaflow.registry import SkillRegistry, SkillSpec

from skills.flaky_test_diagnoser.activities import run_test_subprocess
from skills.flaky_test_diagnoser.workflow import FlakyTestInput, FlakyTestWorkflow


def _load_or_empty(skill: str, name: str, *, substitutions: dict[str, str] | None = None) -> str:
    """Load a prompt from claude-skills, returning '' if the file hasn't been extracted yet."""
    try:
        return load_claude_skill_prompt(skill, name, substitutions=substitutions)
    except PromptNotFoundError:
        return ""


def _build_input(
    *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
) -> FlakyTestInput:
    test = cli_args.get("test") or ""
    if not test:
        raise ValueError("flaky-test-diagnoser requires --arg test='<test identifier>'")
    command = cli_args.get("command") or ""
    if not command:
        raise ValueError("flaky-test-diagnoser requires --arg command='<run command>'")
    try:
        n_runs = int(cli_args.get("n_runs", 10))
    except (TypeError, ValueError):
        n_runs = 10
    return FlakyTestInput(
        run_id=run_id,
        test_identifier=str(test),
        run_dir=run_dir,
        inbox_path=inbox_path,
        run_command=str(command),
        n_runs=n_runs,
        notify=True,
        hyp_system_prompt=_load_or_empty("flaky-test-diagnoser", "hyp.system"),
        hyp_user_prompt=_load_or_empty(
            "flaky-test-diagnoser", "hyp.user",
            substitutions={
                "test_identifier": str(test),
                "fail_rate": "",  # not known until runtime
                "failures": "",
                "total": "",
                "avg_pass_ms": "",
                "avg_fail_ms": "",
            },
        ),
        judge_system_prompt=_load_or_empty("flaky-test-diagnoser", "judge.system"),
        judge_user_prompt=_load_or_empty(
            "flaky-test-diagnoser", "judge.user",
            substitutions={
                "test_identifier": str(test),
                "fail_rate": "",
                "hypotheses_json": "",
            },
        ),
        synth_system_prompt=_load_or_empty("flaky-test-diagnoser", "synth.system"),
        synth_user_prompt=_load_or_empty(
            "flaky-test-diagnoser", "synth.user",
            substitutions={
                "test_identifier": str(test),
                "fail_rate": "",
                "total": "",
                "n_runs": str(n_runs),
                "hypotheses_json": "",
                "rankings_json": "",
            },
        ),
    )


def register(registry: SkillRegistry) -> None:
    registry.register(
        SkillSpec(
            name="flaky-test-diagnoser",
            workflow_cls=FlakyTestWorkflow,
            activities=[
                write_artifact,
                emit_finding,
                spawn_subagent,
                run_test_subprocess,
            ],
            build_input=_build_input,
        )
    )
