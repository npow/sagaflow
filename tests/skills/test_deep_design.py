"""Workflow-level test for DeepDesignWorkflow using time-skipped Temporal env.

The test swaps `spawn_subagent` with a role-dispatching fake that returns
deterministic structured outputs per role. Keeps the test hermetic — no real
Anthropic or claude -p calls.
"""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.deep_design.workflow import DeepDesignInput, DeepDesignWorkflow


@activity.defn(name="spawn_subagent")
async def _fake_spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "draft":
        return {
            "SPEC": (
                "# Cache Invalidation Service\n\n"
                "## Overview\nA distributed cache invalidation service.\n\n"
                "## Goals\n- Fast invalidation propagation\n- At-least-once delivery\n\n"
                "## Non-Goals\n- Cache population\n\n"
                "## Components\n- Coordinator, Workers, Event Bus\n\n"
                "## Failure Modes\n- Network partition: fall back to TTL expiry\n"
            )
        }
    if role == "critic":
        return {
            "FLAWS": json.dumps([
                {
                    "id": "f1",
                    "title": "No backpressure mechanism specified",
                    "severity": "major",
                    "dimension": "scalability and performance",
                    "scenario": "Burst of invalidation events overwhelms workers.",
                },
            ])
        }
    if role == "redesign":
        return {
            "SPEC": (
                "# Cache Invalidation Service (v2)\n\n"
                "## Overview\nA distributed cache invalidation service with backpressure.\n\n"
                "## Goals\n- Fast invalidation propagation\n- At-least-once delivery\n"
                "- Backpressure via bounded queues\n\n"
                "## Non-Goals\n- Cache population\n\n"
                "## Components\n- Coordinator, Workers, Event Bus, Rate Limiter\n\n"
                "## Failure Modes\n- Network partition: fall back to TTL expiry\n"
                "- Queue full: shed load with 503 to producers\n"
            )
        }
    if role == "synth":
        return {
            "REPORT": (
                "# Cache Invalidation Service — Final Spec\n\n"
                "## Overview\nA distributed cache invalidation service.\n\n"
                "## Revision Summary\n- Added backpressure via bounded queues\n"
                "- Added rate limiter component\n"
            )
        }
    return {}


async def test_deep_design_roundtrip_produces_spec(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepDesignWorkflow],
            activities=[write_artifact, emit_finding, _fake_spawn_subagent],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                DeepDesignWorkflow.run,
                DeepDesignInput(
                    run_id="dd-1",
                    concept="A distributed cache invalidation service",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_rounds=1,
                    notify=False,
                ),
                id="dd-1",
                task_queue=TASK_QUEUE,
            )

    # Result summary mentions flaws and spec path.
    assert "flaw" in result
    assert "spec" in result.lower()

    # Final spec.md must exist with meaningful content.
    spec_path = tmp_path / "run" / "spec.md"
    assert spec_path.exists(), f"spec.md not found at {spec_path}"
    spec_text = spec_path.read_text()
    assert "Cache Invalidation" in spec_text

    # INBOX got a DONE entry with the run_id.
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dd-1" in inbox_text
    assert "DONE" in inbox_text
