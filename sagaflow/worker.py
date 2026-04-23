"""Worker daemon: registers all skills and polls the shared task queue."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from sagaflow.paths import Paths
from sagaflow.registry import SkillRegistry
from sagaflow.temporal_client import DEFAULT_NAMESPACE, DEFAULT_TARGET, TASK_QUEUE, connect


_PASSTHROUGH_MODULES = ("httpx", "anthropic", "sagaflow")


def _build_sandbox_runner() -> SandboxedWorkflowRunner:
    """Sandbox runner with httpx/anthropic/sagaflow allowed through import validation."""

    restrictions = SandboxRestrictions.default.with_passthrough_modules(*_PASSTHROUGH_MODULES)
    return SandboxedWorkflowRunner(restrictions=restrictions)


def build_registry() -> SkillRegistry:
    """Import every skill package and register it."""

    registry = SkillRegistry()

    from skills import (
        autopilot,
        deep_debug,
        deep_design,
        deep_plan,
        deep_qa,
        deep_research,
        flaky_test_diagnoser,
        hello_world,
        loop_until_done,
        proposal_reviewer,
        team,
    )

    hello_world.register(registry)
    deep_qa.register(registry)
    deep_debug.register(registry)
    deep_research.register(registry)
    deep_design.register(registry)
    deep_plan.register(registry)
    proposal_reviewer.register(registry)
    loop_until_done.register(registry)
    flaky_test_diagnoser.register(registry)
    team.register(registry)
    autopilot.register(registry)

    # Generic interpreter: runs any claude-skills SKILL.md via Claude tool-use.
    # The workflow module is developed in parallel; if it hasn't landed yet, skip
    # registration so the rest of the worker can still run.
    try:
        from skills import generic as generic_skill
        generic_skill.register(registry)
    except ImportError:
        pass

    return registry


def build_extra_workflows() -> list:
    """Workflow classes beyond the 1-per-skill set tracked by SkillRegistry.

    ``SubagentWorkflow`` is dispatched as a child by ``ClaudeSkillWorkflow`` but
    doesn't correspond to a registered skill. Temporal workers must list every
    workflow class in ``workflows=``, so we collect these here.
    """
    extras: list = []
    try:
        from skills import generic as generic_skill
        extras.extend(generic_skill.extra_workflows())
    except ImportError:
        pass
    return extras


async def _is_worker_reachable(client: Client) -> bool:
    try:
        resp = await client.service_client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(namespace=DEFAULT_NAMESPACE, task_queue=TaskQueue(name=TASK_QUEUE))
        )
    except Exception:  # noqa: BLE001
        return False
    return len(resp.pollers) > 0


async def ensure_worker_running(*, target: str = DEFAULT_TARGET) -> None:
    """If no worker is polling the queue, fork `sagaflow worker run --detached-child`.

    Uses a file lock so concurrent ``sagaflow launch`` invocations don't
    race to spawn multiple workers. The first caller acquires the lock,
    checks reachability, spawns if needed, and releases. Concurrent
    callers block on the lock and re-check reachability after acquiring
    (the first caller's worker is likely already polling by then).
    """
    import fcntl

    client = await connect(target=target)
    if await _is_worker_reachable(client):
        return

    lock_path = Paths.from_env().root / ".worker-spawn.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check after acquiring lock — another process may have spawned
        # the worker while we were waiting.
        if await _is_worker_reachable(client):
            return
        log_dir = Paths.from_env().worker_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"worker-{os.getpid()}.log"
        subprocess.Popen(
            [sys.executable, "-m", "sagaflow.cli", "worker", "run", "--detached-child"],
            stdout=log_file.open("ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Poll until reachable or timeout.
        for _ in range(30):
            await asyncio.sleep(0.5)
            if await _is_worker_reachable(client):
                return
        raise RuntimeError("auto-spawned worker did not become ready within 15s")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


async def run_worker(*, target: str = DEFAULT_TARGET) -> None:
    """Foreground worker. Blocks until process killed."""

    client = await connect(target=target)
    registry = build_registry()
    workflows = list(registry.all_workflows())
    # Seen-set lets us dedupe while preserving order (some extras may already
    # be registered via a skill).
    seen = {id(w) for w in workflows}
    for extra in build_extra_workflows():
        if id(extra) in seen:
            continue
        seen.add(id(extra))
        workflows.append(extra)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=workflows,
        activities=registry.all_activities(),
        workflow_runner=_build_sandbox_runner(),
    )
    await worker.run()
