"""Replay worker: re-executes workflows using pre-recorded cassette data.

Registers a fake ``spawn_subagent`` activity that returns cassette entries
instead of calling LLM. All other activities (write_artifact, emit_finding,
finalize_manifest, Slack progress) run for real.

Usage::

    from sagaflow.replay.worker import run_replay_worker
    asyncio.run(run_replay_worker(cassette_run_dir, skill_name))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from sagaflow.replay.cassette import Cassette, load
from sagaflow.temporal_client import TASK_QUEUE, connect

logger = logging.getLogger(__name__)

_PASSTHROUGH_MODULES = ("httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_", "sniffio")

_replay_cassette: Cassette | None = None
_replay_cursor: int = 0


def _set_cassette(cassette: Cassette) -> None:
    global _replay_cassette, _replay_cursor
    _replay_cassette = cassette
    _replay_cursor = 0


@dataclass(frozen=True)
class _SpawnSubagentInput:
    role: str = ""
    tier_name: str = ""
    system_prompt: str = ""
    user_prompt_path: str = ""
    tools_needed: bool = False
    max_tokens: int = 128_000
    output_schema: dict | None = None
    run_dir: str = ""
    step_index: int = 0


@activity.defn(name="spawn_subagent")
async def replay_spawn_subagent(inp: _SpawnSubagentInput) -> dict[str, str]:
    """Return pre-recorded output from cassette."""
    global _replay_cursor

    if _replay_cassette is None:
        raise RuntimeError("No cassette loaded for replay")

    if _replay_cursor >= len(_replay_cassette.entries):
        raise RuntimeError(
            f"Cassette exhausted at step {_replay_cursor} "
            f"(cassette has {len(_replay_cassette.entries)} entries)"
        )

    entry = _replay_cassette.entries[_replay_cursor]
    _replay_cursor += 1

    logger.info(
        "replay step %d/%d: role=%s tier=%s (%.1fs original)",
        entry.seq + 1,
        len(_replay_cassette.entries),
        entry.role,
        entry.tier,
        entry.duration_seconds,
    )

    if inp.run_dir:
        from sagaflow.manifest import StepRecord, append_step
        append_step(
            Path(inp.run_dir),
            StepRecord(
                step=inp.step_index,
                role=entry.role,
                model=f"replay:{entry.tier}",
                tier=entry.tier,
                input_tokens=int(entry.output.get("_input_tokens", 0)),
                output_tokens=int(entry.output.get("_output_tokens", 0)),
                duration_seconds=0.0,
                status="replay",
            ),
        )

    return entry.output


def _build_sandbox_runner() -> SandboxedWorkflowRunner:
    restrictions = SandboxRestrictions.default.with_passthrough_modules(*_PASSTHROUGH_MODULES)
    return SandboxedWorkflowRunner(restrictions=restrictions)


async def run_replay_worker(
    cassette_run_dir: Path,
    *,
    task_queue: str | None = None,
    target: str | None = None,
) -> None:
    """Start a replay worker that serves one workflow execution, then exits."""
    from sagaflow.worker import build_registry, build_extra_workflows

    cassette = load(cassette_run_dir)
    _set_cassette(cassette)
    logger.info(
        "Loaded cassette: %s (%d entries, skill=%s)",
        cassette.run_id, len(cassette.entries), cassette.skill,
    )

    from sagaflow.temporal_client import DEFAULT_TARGET
    client = await connect(target=target or DEFAULT_TARGET)

    registry = build_registry()
    workflows = list(registry.all_workflows())
    seen = {id(w) for w in workflows}
    for extra in build_extra_workflows():
        if id(extra) not in seen:
            seen.add(id(extra))
            workflows.append(extra)

    real_activities: list[Any] = []
    for act in registry.all_activities():
        defn = getattr(act, "__temporal_activity_definition", None)
        if defn and defn.name == "spawn_subagent":
            continue
        real_activities.append(act)

    from sagaflow.durable.activities import write_artifact, emit_finding
    from sagaflow.durable.activities import finalize_manifest_activity
    from sagaflow.slack_progress import (
        report_slack_progress,
        deliver_artifact_to_slack,
        report_slack_failure,
        report_slack_state_change,
    )

    all_activities: list[Any] = [replay_spawn_subagent]
    seen_names: set[str] = {"spawn_subagent"}
    for act in [
        write_artifact, emit_finding, finalize_manifest_activity,
        report_slack_progress, deliver_artifact_to_slack,
        report_slack_failure, report_slack_state_change,
        *real_activities,
    ]:
        defn = getattr(act, "__temporal_activity_definition", None)
        name = defn.name if defn else f"fn:{id(act)}"
        if name not in seen_names:
            seen_names.add(name)
            all_activities.append(act)

    queue = task_queue or f"{TASK_QUEUE}-replay"
    worker = Worker(
        client,
        task_queue=queue,
        workflows=workflows,
        activities=all_activities,
        workflow_runner=_build_sandbox_runner(),
        debug_mode=True,
    )

    logger.info("Replay worker listening on queue %s", queue)
    await worker.run()
