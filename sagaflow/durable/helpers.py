"""Shared workflow-side helpers — DRY wrappers around common activity dispatch.

Level 1 — single-activity wrappers: ``write``, ``spawn``, ``emit``, ``report_progress``
Level 2 — composite helpers: ``spawn_with_prompt``, ``spawn_parallel``, ``finalize``

These are async functions meant to be called from within a ``@workflow.defn`` class.
They call ``workflow.execute_activity()`` internally, so they require Temporal
workflow context.  Import them under ``workflow.unsafe.imports_passed_through()``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from sagaflow.durable.activities import (
    BudgetCheckInput,
    BudgetCheckResult,
    EmitFindingInput,
    FinalizeManifestInput,
    SdkTelemetryInput,
    SpawnSubagentInput,
    WriteArtifactInput,
)
from sagaflow.durable.retry_policies import HAIKU_POLICY, SONNET_POLICY
from sagaflow.slack_progress import DeliverArtifactInput, ReportSlackProgressInput

logger = logging.getLogger(__name__)

MALFORMED_KEY = "_sagaflow_malformed"

_DEFAULT_WRITE_TIMEOUT = timedelta(seconds=60)
_DEFAULT_SPAWN_TIMEOUT = timedelta(minutes=15)
_DEFAULT_EMIT_TIMEOUT = timedelta(seconds=60)
_DEFAULT_PROGRESS_TIMEOUT = timedelta(seconds=30)
_DEFAULT_DELIVER_TIMEOUT = timedelta(seconds=120)
_DEFAULT_FINALIZE_TIMEOUT = timedelta(seconds=60)
# Under heavy load, utility activities can sit in the queue for minutes waiting
# for a slot while long-running spawn_subagent activities occupy all slots.
# This timeout prevents them from silently queueing forever.
_UTILITY_SCHEDULE_TO_START = timedelta(minutes=5)


async def write(path: str, content: str, *, append: bool = False) -> None:
    """Write (or append to) an artifact file."""
    await workflow.execute_activity(
        "write_artifact",
        WriteArtifactInput(path=path, content=content, append=append),
        start_to_close_timeout=_DEFAULT_WRITE_TIMEOUT,
        schedule_to_start_timeout=_UTILITY_SCHEDULE_TO_START,
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
    enable_working_memory: bool = False,
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
            enable_working_memory=enable_working_memory,
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
        schedule_to_start_timeout=_UTILITY_SCHEDULE_TO_START,
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
            schedule_to_start_timeout=_UTILITY_SCHEDULE_TO_START,
            retry_policy=HAIKU_POLICY,
        )
    except Exception:
        pass
    return steps


# ---------------------------------------------------------------------------
# Level 2 — composite helpers
# ---------------------------------------------------------------------------


async def spawn_with_prompt(
    *,
    role: str,
    tier: str,
    system_prompt: str,
    user_prompt: str,
    run_dir: str,
    suffix: str = "",
    max_tokens: int = 128_000,
    tools_needed: bool = False,
    output_schema: dict | None = None,
    step_index: int = 0,
    mcp_config_path: str | None = None,
    cli_timeout_seconds: float = 3600.0,
    timeout: timedelta = _DEFAULT_SPAWN_TIMEOUT,
    heartbeat: timedelta | None = None,
    retry: RetryPolicy | None = None,
) -> dict[str, str]:
    """Write a prompt file then dispatch an LLM call.

    SDK path (tools_needed=False): routes through sagaflow.engine (Pydantic AI)
    with budget enforcement, cost tracking, manifest recording, and cassette.
    CLI path (tools_needed=True): writes prompt file then spawns via activity.
    """
    if not tools_needed:
        return await _dispatch_sdk(
            role=role,
            tier=tier,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            run_dir=run_dir,
            step_index=step_index,
            timeout=timeout,
        )

    prompt_path = f"{run_dir}/{role}{suffix}-prompt.txt"
    await write(prompt_path, user_prompt)
    return await spawn(
        role=role,
        tier=tier,
        system_prompt=system_prompt,
        prompt_path=prompt_path,
        max_tokens=max_tokens,
        tools_needed=tools_needed,
        output_schema=output_schema,
        run_dir=run_dir,
        step_index=step_index,
        mcp_config_path=mcp_config_path,
        cli_timeout_seconds=cli_timeout_seconds,
        timeout=timeout,
        heartbeat=heartbeat,
        retry=retry,
    )


async def _dispatch_sdk(
    *,
    role: str,
    tier: str,
    system_prompt: str,
    user_prompt: str,
    run_dir: str,
    step_index: int = 0,
    timeout: timedelta = _DEFAULT_SPAWN_TIMEOUT,
) -> dict[str, str]:
    """SDK dispatch via Pydantic AI with budget enforcement and telemetry."""
    effective_tier = tier
    workflow_id = ""
    try:
        workflow_id = workflow.info().workflow_id
    except Exception:
        pass

    if workflow_id:
        budget_result: BudgetCheckResult = await workflow.execute_activity(
            "budget_pre_dispatch",
            BudgetCheckInput(
                workflow_id=workflow_id,
                role=role,
                tier=tier,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            schedule_to_start_timeout=_UTILITY_SCHEDULE_TO_START,
            retry_policy=HAIKU_POLICY,
        )
        if budget_result.abort:
            from sagaflow.budget.enforcer import BudgetExceededError
            raise BudgetExceededError(budget_result.message)
        effective_tier = budget_result.effective_tier

    with workflow.unsafe.imports_passed_through():
        from sagaflow.engine import get_sdk_agent, TIER_TO_MODEL

    agent = get_sdk_agent(
        name=role,
        tier=effective_tier,
        system_prompt=system_prompt,
        timeout=timeout,
    )
    t0 = workflow.now()
    result = await agent.run(user_prompt)
    elapsed = (workflow.now() - t0).total_seconds()

    usage = result.usage()
    input_tokens = usage.request_tokens or 0
    output_tokens = usage.response_tokens or 0
    model = TIER_TO_MODEL.get(effective_tier, f"anthropic:{effective_tier}")

    await workflow.execute_activity(
        "record_sdk_telemetry",
        SdkTelemetryInput(
            role=role,
            tier=effective_tier,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            run_dir=run_dir,
            step_index=step_index,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=elapsed,
            workflow_id=workflow_id,
        ),
        start_to_close_timeout=timedelta(seconds=15),
        schedule_to_start_timeout=_UTILITY_SCHEDULE_TO_START,
        retry_policy=HAIKU_POLICY,
    )

    _token_meta = {
        "_input_tokens": str(input_tokens),
        "_output_tokens": str(output_tokens),
        "_model": model,
    }
    if isinstance(result.output, str):
        return {"RESPONSE": result.output, **_token_meta}
    if hasattr(result.output, "model_dump"):
        d = {k: str(v) for k, v in result.output.model_dump().items()}
        d.update(_token_meta)
        return d
    return {"RESPONSE": str(result.output), **_token_meta}


@dataclass(frozen=True)
class AgentSpec:
    """Specification for one agent in a :func:`spawn_parallel` batch."""

    role: str
    tier: str
    system_prompt: str
    user_prompt: str
    max_tokens: int = 128_000
    tools_needed: bool = False
    output_schema: dict | None = None
    timeout: timedelta = _DEFAULT_SPAWN_TIMEOUT
    heartbeat: timedelta | None = None
    retry: RetryPolicy | None = None


async def spawn_parallel(
    specs: list[AgentSpec],
    run_dir: str,
    *,
    step_index: int = 0,
) -> list[dict[str, str]]:
    """Write prompts and spawn multiple agents in parallel.

    Returns only successful, non-malformed results. Failures and malformed
    responses are logged and skipped — callers get a clean list.
    """
    # Write all prompt files in parallel.
    prompt_paths: list[str] = []
    write_coros = []
    for i, spec in enumerate(specs):
        path = f"{run_dir}/{spec.role}-{i}-prompt.txt"
        prompt_paths.append(path)
        write_coros.append(write(path, spec.user_prompt))
    await asyncio.gather(*write_coros)

    # Spawn all agents in parallel.
    spawn_coros = [
        spawn(
            role=spec.role,
            tier=spec.tier,
            system_prompt=spec.system_prompt,
            prompt_path=prompt_paths[i],
            max_tokens=spec.max_tokens,
            tools_needed=spec.tools_needed,
            output_schema=spec.output_schema,
            run_dir=run_dir,
            step_index=step_index,
            timeout=spec.timeout,
            heartbeat=spec.heartbeat,
            retry=spec.retry,
        )
        for i, spec in enumerate(specs)
    ]
    raw_results = await asyncio.gather(*spawn_coros, return_exceptions=True)

    good: list[dict[str, str]] = []
    for i, result in enumerate(raw_results):
        if isinstance(result, BaseException):
            logger.warning("spawn_parallel: %s failed: %s", specs[i].role, result)
            continue
        if not isinstance(result, dict):
            continue
        if MALFORMED_KEY in result:
            logger.warning("spawn_parallel: %s malformed: %s", specs[i].role, result.get("_error", ""))
            continue
        good.append(result)
    return good


async def finalize(
    *,
    run_dir: str,
    inbox_path: str,
    run_id: str,
    skill: str,
    status: str,
    summary: str,
    termination_label: str = "",
    report_path: str = "",
    notify: bool = True,
) -> None:
    """End-of-workflow sequence: deliver artifact → finalize manifest → emit finding.

    Every orchestration skill ends with this same three-step pattern.
    """
    if report_path:
        try:
            await workflow.execute_activity(
                "deliver_artifact_to_slack",
                DeliverArtifactInput(
                    run_dir=run_dir,
                    artifact_path=report_path,
                    comment=summary,
                ),
                start_to_close_timeout=_DEFAULT_DELIVER_TIMEOUT,
                schedule_to_start_timeout=_UTILITY_SCHEDULE_TO_START,
                retry_policy=HAIKU_POLICY,
            )
        except Exception:
            pass

    await workflow.execute_activity(
        "finalize_manifest",
        FinalizeManifestInput(
            run_dir=run_dir,
            status=status,
            termination_label=termination_label,
        ),
        start_to_close_timeout=_DEFAULT_FINALIZE_TIMEOUT,
        schedule_to_start_timeout=_UTILITY_SCHEDULE_TO_START,
        retry_policy=HAIKU_POLICY,
    )

    await emit(
        inbox_path=inbox_path,
        run_id=run_id,
        skill=skill,
        status=status,
        summary=summary,
        notify=notify,
    )
