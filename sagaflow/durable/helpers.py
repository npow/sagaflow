"""Shared workflow-side helpers — DRY wrappers around common activity dispatch.

Every sagaflow skill workflow needs some combination of write/spawn/emit/progress.
This module provides them so skills don't re-implement the same boilerplate.

These are async functions meant to be called from within a ``@workflow.defn`` class.
They call ``workflow.execute_activity()`` internally, so they require Temporal
workflow context.  Import them under ``workflow.unsafe.imports_passed_through()``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from sagaflow.durable.activities import (
    EmitFindingInput,
    SpawnSubagentInput,
    WriteArtifactInput,
)
from sagaflow.durable.retry_policies import HAIKU_POLICY, SONNET_POLICY
from sagaflow.slack_progress import ReportSlackProgressInput

_DEFAULT_WRITE_TIMEOUT = timedelta(seconds=30)
_DEFAULT_SPAWN_TIMEOUT = timedelta(minutes=15)
_DEFAULT_EMIT_TIMEOUT = timedelta(seconds=30)
_DEFAULT_PROGRESS_TIMEOUT = timedelta(seconds=15)


async def write(path: str, content: str, *, append: bool = False) -> None:
    """Write (or append to) an artifact file."""
    await workflow.execute_activity(
        "write_artifact",
        WriteArtifactInput(path=path, content=content, append=append),
        start_to_close_timeout=_DEFAULT_WRITE_TIMEOUT,
        retry_policy=HAIKU_POLICY,
    )


async def spawn(
    *,
    role: str,
    tier: str,
    system_prompt: str,
    prompt_path: str,
    max_tokens: int = 128_000,
    tools_needed: bool = False,
    output_schema: dict | None = None,
    run_dir: str = "",
    step_index: int = 0,
    mcp_config_path: str | None = None,
    cli_timeout_seconds: float = 3600.0,
    timeout: timedelta = _DEFAULT_SPAWN_TIMEOUT,
    heartbeat: timedelta | None = None,
    retry: RetryPolicy | None = None,
) -> dict[str, str]:
    """Dispatch a subagent and return its parsed structured output."""
    kwargs: dict[str, Any] = {
        "start_to_close_timeout": timeout,
        "retry_policy": retry or SONNET_POLICY,
    }
    if heartbeat is not None:
        kwargs["heartbeat_timeout"] = heartbeat
    result = await workflow.execute_activity(
        "spawn_subagent",
        SpawnSubagentInput(
            role=role,
            tier_name=tier,
            system_prompt=system_prompt,
            user_prompt_path=prompt_path,
            max_tokens=max_tokens,
            tools_needed=tools_needed,
            output_schema=output_schema,
            run_dir=run_dir,
            step_index=step_index,
            mcp_config_path=mcp_config_path,
            cli_timeout_seconds=cli_timeout_seconds,
        ),
        **kwargs,
    )
    return result if isinstance(result, dict) else {}


async def emit(
    *,
    inbox_path: str,
    run_id: str,
    skill: str,
    status: str,
    summary: str,
    notify: bool = True,
) -> None:
    """Emit a finding to the inbox."""
    await workflow.execute_activity(
        "emit_finding",
        EmitFindingInput(
            inbox_path=inbox_path,
            run_id=run_id,
            skill=skill,
            status=status,
            summary=summary,
            notify=notify,
            timestamp_iso=workflow.now().isoformat(timespec="seconds"),
        ),
        start_to_close_timeout=_DEFAULT_EMIT_TIMEOUT,
        retry_policy=HAIKU_POLICY,
    )


async def report_progress(
    run_dir: str,
    title: str,
    phases: list[str],
    phase_idx: int,
    status: str = "in_progress",
    detail: str = "",
    final: bool = False,
    *,
    steps: list[dict] | None = None,
) -> list[dict]:
    """Report phase progress to Slack. Returns the (possibly initialised) steps list."""
    if steps is None:
        steps = [
            {"name": n, "status": "pending", "detail": "", "elapsed_s": 0.0}
            for n in phases
        ]
    steps[phase_idx]["status"] = status
    if detail:
        steps[phase_idx]["detail"] = detail
    try:
        await workflow.execute_activity(
            "report_slack_progress",
            ReportSlackProgressInput(
                run_dir=run_dir,
                title=title,
                steps=tuple(steps),
                final=final,
            ),
            start_to_close_timeout=_DEFAULT_PROGRESS_TIMEOUT,
            retry_policy=HAIKU_POLICY,
        )
    except Exception:
        pass
    return steps
