"""Workflow-level test for DeepPlanWorkflow using time-skipped Temporal env.

The test swaps `spawn_subagent` with a role-dispatching fake that returns
deterministic structured outputs per role. Keeps the test hermetic — no real
Anthropic or claude -p calls.
"""

from __future__ import annotations

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.deep_plan.workflow import DeepPlanInput, DeepPlanWorkflow


@activity.defn(name="spawn_subagent")
async def _fake_spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "planner":
        return {
            "PLAN": "# Plan\n\nStep 1: Do the thing.\nStep 2: Verify.\n",
            "ACCEPTANCE_CRITERIA": "[]",
        }
    if role == "architect":
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "critic":
        # Immediately approve to trigger consensus on iteration 0.
        return {"VERDICT": "APPROVE"}
    if role == "adr":
        return {"ADR": "# ADR\n\n## Status\nAccepted\n\n## Decision\nProceed as planned.\n"}
    return {}


async def test_deep_plan_roundtrip_produces_plan_and_adr(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepPlanWorkflow],
            activities=[write_artifact, emit_finding, _fake_spawn_subagent],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                DeepPlanWorkflow.run,
                DeepPlanInput(
                    task="Build a distributed rate-limiter",
                    run_id="dp-1",
                    run_dir=str(tmp_path / "run"),
                    inbox_path=str(tmp_path / "INBOX.md"),
                    max_iter=5,
                    notify=False,
                ),
                id="dp-1",
                task_queue=TASK_QUEUE,
            )

    # Result summary mentions the terminal label.
    assert "consensus_reached" in result

    # plan.md must exist and contain plan content.
    plan_path = tmp_path / "run" / "plan.md"
    assert plan_path.exists(), "plan.md was not written"
    assert "Plan" in plan_path.read_text()

    # adr.md must exist (consensus was reached).
    adr_path = tmp_path / "run" / "adr.md"
    assert adr_path.exists(), "adr.md was not written"
    assert "ADR" in adr_path.read_text()

    # INBOX got an entry for this run_id with consensus in the summary.
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dp-1" in inbox_text
    assert "consensus_reached" in inbox_text
