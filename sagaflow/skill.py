"""High-level Skill base class — zero Temporal knowledge required.

Skill authors subclass :class:`Skill`, define ``name``, ``phases``, and
implement ``async def run(self, ...)``.  Sagaflow handles everything else:
Temporal workflow generation, activity dispatch, prompt file management,
progress reporting, manifest finalization, inbox emission, Slack delivery.

Usage::

    from sagaflow import Skill, Agent

    class DeepCodeReview(Skill):
        name = "deep-code-review"
        phases = ["Snapshot", "Critique", "Synthesize"]

        async def run(self, task: str):
            self.progress(0, "snapshotting")
            await self.write("snapshot.md", task)

            self.progress(1, "critiquing")
            results = await self.parallel([
                Agent("security", tier="HAIKU", prompt="..."),
                Agent("perf", tier="HAIKU", prompt="..."),
            ])

            self.progress(2, "synthesizing")
            report = await self.agent("synth", tier="SONNET", prompt="...")

            return report.get("SUMMARY", "done")
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Agent:
    """Specification for a single agent dispatch."""

    role: str
    tier: str = "SONNET"
    prompt: str = ""
    system_prompt: str = ""
    max_tokens: int = 128_000
    tools_needed: bool = False
    output_schema: dict | None = None
    timeout_minutes: float = 15.0


class Skill:
    """Base class for sagaflow skills.  Subclass and implement ``run()``.

    Class attributes:
        name:   Skill name (used in ``sagaflow launch <name>``).
        phases: List of phase names for progress reporting.

    The ``run()`` method signature defines CLI args.  Parameters beyond
    ``self`` become ``--arg <name>=...`` flags.  The first required
    parameter also accepts positional text.

    Instance attributes available inside ``run()``:
        self.run_dir, self.run_id, self.inbox_path
    """

    name: str = ""
    phases: list[str] = []

    run_dir: str = ""
    run_id: str = ""
    inbox_path: str = ""
    notify: bool = True
    _steps: list[dict] | None = None

    # -----------------------------------------------------------------
    # Agent dispatch
    # -----------------------------------------------------------------

    async def agent(
        self,
        role: str,
        *,
        tier: str = "SONNET",
        prompt: str = "",
        system_prompt: str = "",
        max_tokens: int = 128_000,
        tools_needed: bool = False,
        output_schema: dict | None = None,
        timeout_minutes: float = 15.0,
    ) -> dict[str, str]:
        """Dispatch a single agent. Handles prompt file creation internally."""
        from sagaflow.durable.helpers import spawn_with_prompt

        return await spawn_with_prompt(
            role=role,
            tier=tier,
            system_prompt=system_prompt or f"You are a {role} agent.",
            user_prompt=prompt,
            run_dir=self.run_dir,
            max_tokens=max_tokens,
            tools_needed=tools_needed,
            output_schema=output_schema,
            timeout=timedelta(minutes=timeout_minutes),
        )

    async def parallel(self, agents: list[Agent]) -> list[dict[str, str]]:
        """Dispatch multiple agents in parallel. Returns successful results only."""
        from sagaflow.durable.helpers import AgentSpec, spawn_parallel

        specs = [
            AgentSpec(
                role=a.role,
                tier=a.tier,
                system_prompt=a.system_prompt or f"You are a {a.role} agent.",
                user_prompt=a.prompt,
                max_tokens=a.max_tokens,
                tools_needed=a.tools_needed,
                output_schema=a.output_schema,
                timeout=timedelta(minutes=a.timeout_minutes),
            )
            for a in agents
        ]
        return await spawn_parallel(specs, self.run_dir)

    # -----------------------------------------------------------------
    # Artifact I/O
    # -----------------------------------------------------------------

    async def write(self, filename: str, content: str, *, append: bool = False) -> str:
        """Write a file to the run directory. Returns the full path."""
        from sagaflow.durable.helpers import write as _write

        path = f"{self.run_dir}/{filename}"
        await _write(path, content, append=append)
        return path

    # -----------------------------------------------------------------
    # Progress
    # -----------------------------------------------------------------

    def progress(self, phase_idx: int, detail: str = "") -> None:
        """Mark a phase as in-progress (prior phases auto-completed)."""
        if self._steps is None:
            self._steps = [
                {"name": n, "status": "pending", "detail": "", "elapsed_s": 0.0}
                for n in self.phases
            ]
        for i, step in enumerate(self._steps):
            if i < phase_idx:
                step["status"] = "completed"
            elif i == phase_idx:
                step["status"] = "in_progress"
                if detail:
                    step["detail"] = detail

    async def _flush_progress(self, *, final: bool = False) -> None:
        if self._steps is None:
            return
        from sagaflow.durable.helpers import report_progress

        last_active = 0
        for i, step in enumerate(self._steps):
            if step["status"] in ("in_progress", "completed"):
                last_active = i
        await report_progress(
            self.run_dir, self.name, self.phases, last_active,
            steps=self._steps, final=final,
        )


# =====================================================================
# Framework: shared Temporal workflow + registry + auto-registration
# =====================================================================

_SKILL_CLASSES: dict[str, type[Skill]] = {}


@dataclass(frozen=True)
class SkillInput:
    """Generic input for all Skill-based workflows."""

    skill_name: str
    run_id: str = ""
    inbox_path: str = ""
    run_dir: str = ""
    notify: bool = True
    user_args: str = ""  # JSON-encoded dict of user arguments


@workflow.defn(name="sagaflow-skill")
class SkillWorkflow:
    """Single shared Temporal workflow that dispatches to any Skill subclass."""

    @workflow.run
    async def run(self, inp: SkillInput) -> str:
        import json

        skill_cls = _SKILL_CLASSES.get(inp.skill_name)
        if skill_cls is None:
            raise ValueError(f"Unknown skill: {inp.skill_name!r}")

        skill = skill_cls()
        skill.run_dir = inp.run_dir
        skill.run_id = inp.run_id
        skill.inbox_path = inp.inbox_path
        skill.notify = inp.notify

        user_args: dict[str, Any] = {}
        if inp.user_args:
            user_args = json.loads(inp.user_args)

        # Map user_args to run() parameters with type coercion
        sig = inspect.signature(skill_cls.run)
        kwargs: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            if param_name in user_args:
                val = user_args[param_name]
                ann = param.annotation
                if ann in (int, "int"):
                    val = int(val)
                elif ann in (float, "float"):
                    val = float(val)
                elif ann in (bool, "bool"):
                    val = str(val).lower() not in ("false", "0", "no", "")
                kwargs[param_name] = val
            elif param.default is not inspect.Parameter.empty:
                pass  # use default
            else:
                raise ValueError(
                    f"{skill_cls.name} requires '{param_name}' "
                    f"(pass via --arg {param_name}='...')"
                )

        if skill.phases:
            await skill._flush_progress()

        try:
            result = await skill.run(**kwargs)
        except Exception as exc:
            from sagaflow.durable.helpers import finalize

            await finalize(
                run_dir=skill.run_dir,
                inbox_path=skill.inbox_path,
                run_id=skill.run_id,
                skill=skill.name,
                status="FAILED",
                summary=f"{skill.name} failed: {type(exc).__name__}: {exc}",
                termination_label="error",
                notify=skill.notify,
            )
            raise

        summary = result if isinstance(result, str) else str(result)

        if skill.phases:
            for step in skill._steps or []:
                step["status"] = "completed"
            await skill._flush_progress(final=True)

        from sagaflow.durable.helpers import finalize

        await finalize(
            run_dir=skill.run_dir,
            inbox_path=skill.inbox_path,
            run_id=skill.run_id,
            skill=skill.name,
            status="DONE",
            summary=summary,
            termination_label="complete",
            notify=skill.notify,
        )
        return summary


def register_skill(skill_cls: type[Skill]) -> dict[str, Any]:
    """Register a Skill subclass with sagaflow.  Returns registration metadata."""
    import json

    from sagaflow.durable.activities import (
        emit_finding,
        finalize_manifest_activity,
        run_shell_activity,
        spawn_subagent,
        write_artifact,
    )
    from sagaflow.registry import SkillSpec
    from sagaflow.slack_progress import (
        deliver_artifact_to_slack,
        report_slack_progress,
    )

    _SKILL_CLASSES[skill_cls.name] = skill_cls

    # Build _build_input that maps cli_args → SkillInput with JSON user_args
    sig = inspect.signature(skill_cls.run)
    user_params = [
        (name, p)
        for name, p in sig.parameters.items()
        if name != "self"
    ]
    primary_name = None
    for name, p in user_params:
        if p.default is inspect.Parameter.empty:
            primary_name = name
            break

    def _build_input(
        *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any],
        _skill_name: str = skill_cls.name,
        _primary: str | None = primary_name,
    ) -> SkillInput:
        user_args: dict[str, Any] = {}
        for name, _ in user_params:
            if name in cli_args:
                user_args[name] = cli_args[name]
        if _primary and _primary not in user_args:
            extra = cli_args.get("_extra")
            if isinstance(extra, list) and extra:
                user_args[_primary] = " ".join(str(x) for x in extra)
        if _primary and _primary not in user_args:
            raise ValueError(
                f"{_skill_name} requires '{_primary}' "
                f"(pass via --arg {_primary}='...' or as positional text)"
            )
        return SkillInput(
            skill_name=_skill_name,
            run_id=run_id,
            inbox_path=inbox_path,
            run_dir=run_dir,
            user_args=json.dumps(user_args),
        )

    spec = SkillSpec(
        name=skill_cls.name,
        workflow_cls=SkillWorkflow,
        activities=[
            write_artifact, emit_finding, spawn_subagent,
            report_slack_progress, deliver_artifact_to_slack,
            finalize_manifest_activity, run_shell_activity,
        ],
        build_input=_build_input,
    )

    return {"spec": spec, "workflow_cls": SkillWorkflow}
