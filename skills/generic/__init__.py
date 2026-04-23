"""Generic interpreter skill — runs any claude-skills SKILL.md via Claude tool-use loop.

This skill lets ``sagaflow launch <skill>`` dispatch to a single ``ClaudeSkillWorkflow``
for any skill that exists in ``~/.claude/skills/<skill>/SKILL.md`` but does not have a
bespoke sagaflow workflow. The CLI performs fallback dispatch: if ``<skill>`` is not
registered, it checks the claude-skills dir and, if a ``SKILL.md`` is present, rewrites
the target to the generic workflow with ``_target_skill=<skill>`` stashed in args.

The workflow itself (:mod:`sagaflow.generic.workflow`) is developed in parallel. This
module tolerates its absence at import time so the registry can load even before the
workflow lands -- ``register()`` will raise a clear ``ImportError`` if invoked before
the workflow module exists.
"""

from __future__ import annotations

from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.generic.activities import (
    bash_tool,
    call_claude_with_tools,
    generic_tool_activities,
    glob_tool,
    grep_tool,
    read_file_tool,
)
from sagaflow.prompts import claude_skills_dir
from sagaflow.registry import SkillRegistry, SkillSpec

# The workflow module may not exist yet while the generic interpreter is under
# parallel development. Try/except keeps this module importable; register() will
# surface a clean error if the worker tries to spin up without the workflow.
try:
    from sagaflow.generic.workflow import (  # type: ignore[import-not-found]
        ClaudeSkillInput,
        ClaudeSkillWorkflow,
        SubagentWorkflow,
    )
    _WORKFLOW_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - covered once workflow lands
    ClaudeSkillInput = None  # type: ignore[assignment,misc]
    ClaudeSkillWorkflow = None  # type: ignore[assignment,misc]
    SubagentWorkflow = None  # type: ignore[assignment,misc]
    _WORKFLOW_IMPORT_ERROR = exc


def _build_input(
    *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
) -> "ClaudeSkillInput":
    """Translate CLI args into a ``ClaudeSkillInput``.

    The CLI fallback dispatcher sets ``_target_skill`` to the user-requested skill
    name. We load the matching ``SKILL.md`` from the claude-skills dir and pass it
    through as the system prompt body.
    """
    if ClaudeSkillInput is None:  # pragma: no cover - see module-level try/except
        raise _WORKFLOW_IMPORT_ERROR or ImportError(
            "sagaflow.generic.workflow not importable; cannot build ClaudeSkillInput"
        )

    target = str(cli_args.get("_target_skill", "")).strip()
    if not target:
        raise ValueError(
            "generic workflow requires _target_skill (set by CLI fallback dispatch)"
        )
    skill_md_path = claude_skills_dir() / target / "SKILL.md"
    if not skill_md_path.exists():
        raise ValueError(
            f"no SKILL.md found for skill {target!r} at {skill_md_path}"
        )
    skill_md = skill_md_path.read_text(encoding="utf-8")
    # Strip out _target_skill from user_args; keep everything else as-is.
    user_args = {k: str(v) for k, v in cli_args.items() if k != "_target_skill"}
    # Coerce max_iterations if provided.
    try:
        max_iter = int(cli_args.get("max_iterations", 50))
    except (TypeError, ValueError):
        max_iter = 50
    tier = str(cli_args.get("tier", "SONNET")).upper()
    return ClaudeSkillInput(
        run_id=run_id,
        run_dir=run_dir,
        inbox_path=inbox_path,
        skill_name=target,
        skill_md_content=skill_md,
        user_args=user_args,
        max_iterations=max_iter,
        tier_name=tier,
        notify=True,
    )


def register(registry: SkillRegistry) -> None:
    """Register the generic interpreter with the sagaflow skill registry.

    Raises ``ImportError`` if :mod:`sagaflow.generic.workflow` hasn't landed yet;
    the worker can't run the generic skill without it.
    """
    if ClaudeSkillWorkflow is None:
        raise ImportError(
            "sagaflow.generic.workflow is not importable; cannot register generic "
            "skill. Ensure ClaudeSkillWorkflow + SubagentWorkflow exist."
        ) from _WORKFLOW_IMPORT_ERROR
    # generic_tool_activities contains the `generic_tool_adapter__*` wrappers that
    # accept Claude's raw tool_use dict and forward to the typed activities above.
    # The workflow dispatches adapters by name (not the typed activities directly)
    # because Claude's tool_use.input is always a dict, not our frozen dataclasses.
    registry.register(
        SkillSpec(
            name="generic",
            workflow_cls=ClaudeSkillWorkflow,
            activities=[
                write_artifact,
                emit_finding,
                spawn_subagent,
                call_claude_with_tools,
                read_file_tool,
                bash_tool,
                grep_tool,
                glob_tool,
                *generic_tool_activities,
            ],
            build_input=_build_input,
        )
    )


def extra_workflows() -> list[Any]:
    """Additional workflow classes the worker must know about.

    ``SkillSpec`` tracks only one workflow per skill, but the generic interpreter
    dispatches ``SubagentWorkflow`` as a child. Temporal workers need every workflow
    class in their ``workflows=`` list, so ``build_registry()`` calls this helper to
    augment the set returned by ``registry.all_workflows()``.
    """
    if SubagentWorkflow is None:
        return []
    return [SubagentWorkflow]
