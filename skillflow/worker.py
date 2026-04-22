"""Worker daemon: registers all skills and polls the shared task queue."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from skillflow.paths import Paths
from skillflow.registry import SkillRegistry
from skillflow.temporal_client import DEFAULT_NAMESPACE, DEFAULT_TARGET, TASK_QUEUE, connect


_PASSTHROUGH_MODULES = ("httpx", "anthropic", "skillflow")


def _build_sandbox_runner() -> SandboxedWorkflowRunner:
    """Sandbox runner with httpx/anthropic/skillflow allowed through import validation."""

    restrictions = SandboxRestrictions.default.with_passthrough_modules(*_PASSTHROUGH_MODULES)
    return SandboxedWorkflowRunner(restrictions=restrictions)


def build_registry() -> SkillRegistry:
    """Import every skill package and register it."""

    registry = SkillRegistry()

    from skills import hello_world

    hello_world.register(registry)
    return registry


async def _is_worker_reachable(client: Client) -> bool:  # type: ignore[type-arg]
    try:
        resp = await client.service_client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(namespace=DEFAULT_NAMESPACE, task_queue={"name": TASK_QUEUE})
        )
    except Exception:  # noqa: BLE001
        return False
    return len(resp.pollers) > 0


async def ensure_worker_running(*, target: str = DEFAULT_TARGET) -> None:
    """If no worker is polling the queue, fork `skillflow worker run --detached-child`."""

    client = await connect(target=target)
    if await _is_worker_reachable(client):
        return
    log_dir = Paths.from_env().worker_log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"worker-{os.getpid()}.log"
    subprocess.Popen(
        [sys.executable, "-m", "skillflow.cli", "worker", "run", "--detached-child"],
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


async def run_worker(*, target: str = DEFAULT_TARGET) -> None:
    """Foreground worker. Blocks until process killed."""

    client = await connect(target=target)
    registry = build_registry()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=registry.all_workflows(),
        activities=registry.all_activities(),
        workflow_runner=_build_sandbox_runner(),
    )
    await worker.run()
