"""Public API for sagaflow — Output-style workflow authoring in Python.

Skill authors use ``workflow()``, ``generate_text()``, ``parallel()``,
and ``.prompt`` files.  Temporal, activities, retries, manifests, inbox,
and Slack delivery are invisible.

Usage::

    from sagaflow import workflow, generate_text, parallel, write_file

    @workflow(name="deep-code-review", phases=["Critique", "Judge", "Synthesize"])
    async def run(task: str, max_rounds: int = 3):
        results = await parallel(
            generate_text("security-critic", variables={"task": task}),
            generate_text("perf-critic", variables={"task": task}),
        )
        verdict = await generate_text("judge", variables={"findings": str(results)})
        return verdict

Prompts live in ``prompts/<name>.prompt`` next to ``workflow.py``::

    ---
    tier: HAIKU
    max_tokens: 8192
    ---
    <system>
    You are a security reviewer. Find vulnerabilities.
    </system>
    <user>
    Review this code: {{ task }}
    </user>
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from string import Template
from typing import Any, Callable, Coroutine

import yaml
from temporalio import workflow as _tw

logger = logging.getLogger(__name__)

# Module-level registry — populated by @workflow decorator, read by worker.
_WORKFLOW_REGISTRY: dict[str, _WorkflowDef] = {}


class _WorkflowDef:
    """Internal representation of a registered workflow."""

    __slots__ = ("name", "phases", "fn", "prompts_dir")

    def __init__(
        self,
        name: str,
        phases: list[str],
        fn: Callable[..., Coroutine[Any, Any, Any]],
        prompts_dir: Path | None,
    ):
        self.name = name
        self.phases = phases
        self.fn = fn
        self.prompts_dir = prompts_dir


# =====================================================================
# @workflow decorator
# =====================================================================

def workflow(
    name: str,
    *,
    phases: list[str] | None = None,
) -> Callable[..., Any]:
    """Decorator that registers a function as a sagaflow workflow.

    The decorated function's signature defines CLI args::

        @workflow(name="my-skill", phases=["Plan", "Execute"])
        async def run(task: str, max_rounds: int = 3):
            ...

    ``task`` becomes a required arg, ``max_rounds`` optional with default 3.
    """
    def decorator(fn: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
        # Resolve prompts/ dir relative to the file that defines the workflow
        caller_file = inspect.getfile(fn)
        prompts_dir = Path(caller_file).parent / "prompts"

        defn = _WorkflowDef(
            name=name,
            phases=phases or [],
            fn=fn,
            prompts_dir=prompts_dir if prompts_dir.is_dir() else None,
        )
        _WORKFLOW_REGISTRY[name] = defn
        fn._sagaflow_workflow = defn  # type: ignore[attr-defined]
        return fn

    return decorator


# =====================================================================
# generate_text — load prompt + dispatch LLM
# =====================================================================

class _PromptConfig:
    """Parsed .prompt file."""

    __slots__ = ("tier", "max_tokens", "tools_needed", "system", "user_template")

    def __init__(
        self,
        tier: str = "SONNET",
        max_tokens: int = 128_000,
        tools_needed: bool = False,
        system: str = "",
        user_template: str = "",
    ):
        self.tier = tier
        self.max_tokens = max_tokens
        self.tools_needed = tools_needed
        self.system = system
        self.user_template = user_template


def _parse_prompt_file(path: Path) -> _PromptConfig:
    """Parse a .prompt file: YAML frontmatter + <system>/<user> blocks."""
    raw = path.read_text(encoding="utf-8")

    # Extract YAML frontmatter
    config: dict[str, Any] = {}
    body = raw
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if fm_match:
        config = yaml.safe_load(fm_match.group(1)) or {}
        body = raw[fm_match.end() :]

    # Extract <system> and <user> blocks
    system = ""
    user = ""
    sys_match = re.search(r"<system>\s*\n?(.*?)\n?\s*</system>", body, re.DOTALL)
    if sys_match:
        system = sys_match.group(1).strip()
    user_match = re.search(r"<user>\s*\n?(.*?)\n?\s*</user>", body, re.DOTALL)
    if user_match:
        user = user_match.group(1).strip()

    if not system and not user:
        user = body.strip()

    return _PromptConfig(
        tier=str(config.get("tier", config.get("model", "SONNET"))).upper(),
        max_tokens=int(config.get("max_tokens", config.get("maxTokens", 128_000))),
        tools_needed=bool(config.get("tools_needed", config.get("tools", False))),
        system=system,
        user_template=user,
    )


# Resolve a prompt name to a config, searching the workflow's prompts dir.
_prompt_cache: dict[str, _PromptConfig] = {}


def _resolve_prompt(
    prompt_name: str, prompts_dir: Path | None
) -> _PromptConfig:
    cache_key = f"{prompts_dir}:{prompt_name}"
    if cache_key in _prompt_cache:
        return _prompt_cache[cache_key]

    if prompts_dir:
        for ext in (".prompt", ".md", ".txt", ""):
            candidate = prompts_dir / f"{prompt_name}{ext}"
            if candidate.exists():
                config = _parse_prompt_file(candidate)
                _prompt_cache[cache_key] = config
                return config

    # Fallback: use prompt_name as the system prompt directly
    config = _PromptConfig(system=f"You are a {prompt_name} agent.", user_template="")
    _prompt_cache[cache_key] = config
    return config


async def generate_text(
    prompt: str,
    *,
    variables: dict[str, Any] | None = None,
    tier: str | None = None,
    max_tokens: int | None = None,
    tools_needed: bool | None = None,
    system_prompt: str | None = None,
    output_schema: dict[str, Any] | None = None,
    timeout_minutes: float = 15.0,
) -> dict[str, str]:
    """Call an LLM using a prompt file or inline prompt.

    ``prompt`` is either:
    - A prompt file name (e.g. ``"security-critic"``) resolved from ``prompts/``)
    - An inline prompt string (if no matching file found)

    ``variables`` are substituted into the prompt template using ``$var`` syntax.
    """
    from sagaflow.durable.helpers import spawn_with_prompt

    # Get the calling workflow's context
    ctx = _get_context()

    # Resolve prompt config from file or inline
    config = _resolve_prompt(prompt, ctx.prompts_dir)

    # Apply template variables
    user_text = config.user_template
    if variables:
        try:
            user_text = Template(user_text).safe_substitute(variables)
        except Exception:
            for k, v in variables.items():
                user_text = user_text.replace(f"{{{{ {k} }}}}", str(v))
                user_text = user_text.replace(f"${k}", str(v))

    # If no user template, use the variables dict as the prompt
    if not user_text and variables:
        user_text = "\n".join(f"{k}: {v}" for k, v in variables.items())

    # Allow explicit overrides
    effective_tier = tier or config.tier
    effective_max_tokens = max_tokens or config.max_tokens
    effective_tools = tools_needed if tools_needed is not None else config.tools_needed
    effective_system = system_prompt or config.system

    role = prompt.split("@")[0]  # strip version suffix like "summarize@v1"

    return await spawn_with_prompt(
        role=role,
        tier=effective_tier,
        system_prompt=effective_system,
        user_prompt=user_text,
        run_dir=ctx.run_dir,
        max_tokens=effective_max_tokens,
        tools_needed=effective_tools,
        output_schema=output_schema,
        timeout=timedelta(minutes=timeout_minutes),
    )


# =====================================================================
# parallel — dispatch multiple generate_text calls concurrently
# =====================================================================

async def parallel(*coros_or_awaitables: Any) -> list[dict[str, str]]:
    """Run multiple generate_text calls in parallel, return successful results.

    Usage::

        results = await parallel(
            generate_text("critic-1", variables={"task": task}),
            generate_text("critic-2", variables={"task": task}),
        )
    """
    raw = await asyncio.gather(*coros_or_awaitables, return_exceptions=True)
    good: list[dict[str, str]] = []
    for result in raw:
        if isinstance(result, BaseException):
            logger.warning("parallel: agent failed: %s", result)
            continue
        if isinstance(result, dict) and "_sagaflow_malformed" not in result:
            good.append(result)
    return good


# =====================================================================
# write_file — write an artifact to the run directory
# =====================================================================

async def write_file(filename: str, content: str, *, append: bool = False) -> str:
    """Write a file to the run directory. Returns the full path."""
    from sagaflow.durable.helpers import write

    ctx = _get_context()
    path = f"{ctx.run_dir}/{filename}"
    await write(path, content, append=append)
    return path


# =====================================================================
# progress — update phase progress
# =====================================================================

async def progress(phase_idx: int, detail: str = "") -> None:
    """Report progress for the current phase."""
    from sagaflow.durable.helpers import report_progress

    ctx = _get_context()
    if ctx.steps is None:
        ctx.steps = [
            {"name": n, "status": "pending", "detail": "", "elapsed_s": 0.0}
            for n in ctx.phases
        ]
    for i, step in enumerate(ctx.steps):
        if i < phase_idx:
            step["status"] = "completed"
        elif i == phase_idx:
            step["status"] = "in_progress"
            if detail:
                step["detail"] = detail
    await report_progress(
        ctx.run_dir, ctx.name, ctx.phases, phase_idx,
        steps=ctx.steps,
    )


# =====================================================================
# Execution context — threaded through the workflow run
# =====================================================================

class _WorkflowContext:
    __slots__ = ("name", "run_dir", "run_id", "inbox_path", "notify",
                 "phases", "prompts_dir", "steps")

    def __init__(self) -> None:
        self.name = ""
        self.run_dir = ""
        self.run_id = ""
        self.inbox_path = ""
        self.notify = True
        self.phases: list[str] = []
        self.prompts_dir: Path | None = None
        self.steps: list[dict[str, Any]] | None = None


_current_context: _WorkflowContext | None = None


def _get_context() -> _WorkflowContext:
    if _current_context is None:
        raise RuntimeError("sagaflow API called outside of a workflow run")
    return _current_context


# =====================================================================
# Temporal workflow class — shared by all @workflow-decorated functions
# =====================================================================


@dataclass(frozen=True)
class ApiWorkflowInput:
    skill_name: str
    run_id: str = ""
    inbox_path: str = ""
    run_dir: str = ""
    notify: bool = True
    user_args: str = ""


@_tw.defn(name="sagaflow-api-workflow")
class ApiWorkflow:
    """Shared Temporal workflow for all @workflow-decorated functions."""

    @_tw.run
    async def run(self, inp: "ApiWorkflowInput") -> str:
        global _current_context

        defn = _WORKFLOW_REGISTRY.get(inp.skill_name)
        if defn is None:
            raise ValueError(f"Unknown workflow: {inp.skill_name!r}")

        ctx = _WorkflowContext()
        ctx.name = defn.name
        ctx.run_dir = inp.run_dir
        ctx.run_id = inp.run_id
        ctx.inbox_path = inp.inbox_path
        ctx.notify = inp.notify
        ctx.phases = defn.phases
        ctx.prompts_dir = defn.prompts_dir
        _current_context = ctx

        user_args: dict[str, Any] = json.loads(inp.user_args) if inp.user_args else {}

        # Map user_args to fn parameters with type coercion
        sig = inspect.signature(defn.fn)
        kwargs: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
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
            elif param.default is inspect.Parameter.empty:
                raise ValueError(
                    f"{defn.name} requires '{param_name}' "
                    f"(pass via --arg {param_name}='...')"
                )

        if ctx.phases:
            await progress(0)

        try:
            result = await defn.fn(**kwargs)
        except Exception as exc:
            from sagaflow.durable.helpers import finalize

            await finalize(
                run_dir=ctx.run_dir,
                inbox_path=ctx.inbox_path,
                run_id=ctx.run_id,
                skill=ctx.name,
                status="FAILED",
                summary=f"{ctx.name} failed: {type(exc).__name__}: {exc}",
                termination_label="error",
                notify=ctx.notify,
            )
            raise
        finally:
            _current_context = None

        summary = result if isinstance(result, str) else str(result)

        if ctx.phases and ctx.steps:
            for step in ctx.steps:
                step["status"] = "completed"
            from sagaflow.durable.helpers import report_progress as _rp

            await _rp(
                ctx.run_dir, ctx.name, ctx.phases, len(ctx.phases) - 1,
                steps=ctx.steps, final=True,
            )

        from sagaflow.durable.helpers import finalize

        await finalize(
            run_dir=ctx.run_dir,
            inbox_path=ctx.inbox_path,
            run_id=ctx.run_id,
            skill=ctx.name,
            status="DONE",
            summary=summary,
            termination_label="complete",
            notify=ctx.notify,
        )
        return summary


# =====================================================================
# Registration — called by worker to register workflows
# =====================================================================

def register_api_workflows(registry: Any) -> None:
    """Register all @workflow-decorated functions with the sagaflow registry."""
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

    standard_activities: list[Callable[..., Any]] = [
        write_artifact, emit_finding, spawn_subagent,
        report_slack_progress, deliver_artifact_to_slack,
        finalize_manifest_activity, run_shell_activity,
    ]

    for defn in _WORKFLOW_REGISTRY.values():
        sig = inspect.signature(defn.fn)
        params = [
            (name, p)
            for name, p in sig.parameters.items()
        ]
        primary_name = None
        for pname, p in params:
            if p.default is inspect.Parameter.empty:
                primary_name = pname
                break

        def _build_input(
            *, run_id: str, run_dir: str, inbox_path: str,
            cli_args: dict[str, Any],
            _skill_name: str = defn.name,
            _primary: str | None = primary_name,
            _params: list[tuple[str, inspect.Parameter]] = params,
        ) -> ApiWorkflowInput:
            user_args: dict[str, Any] = {}
            for pname, _ in _params:
                if pname in cli_args:
                    user_args[pname] = cli_args[pname]
            if _primary and _primary not in user_args:
                extra = cli_args.get("_extra")
                if isinstance(extra, list) and extra:
                    user_args[_primary] = " ".join(str(x) for x in extra)
            if _primary and _primary not in user_args:
                raise ValueError(
                    f"{_skill_name} requires '{_primary}' "
                    f"(pass via --arg {_primary}='...' or as positional text)"
                )
            return ApiWorkflowInput(
                skill_name=_skill_name,
                run_id=run_id,
                inbox_path=inbox_path,
                run_dir=run_dir,
                user_args=json.dumps(user_args),
            )

        if defn.name in registry.names():
            continue

        registry.register(
            SkillSpec(
                name=defn.name,
                workflow_cls=ApiWorkflow,
                activities=standard_activities,
                build_input=_build_input,
            )
        )
