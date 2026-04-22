"""Workflow-level test for DeepDebugWorkflow."""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.deep_debug.workflow import DeepDebugInput, DeepDebugWorkflow


@activity.defn(name="spawn_subagent")
async def _fake(inp: SpawnSubagentInput) -> dict[str, str]:
    if inp.role == "hypothesis":
        return {
            "DIMENSION": "concurrency",
            "MECHANISM": "Shared state read during a write.",
            "EVIDENCE_TIER": "3",
        }
    if inp.role == "judge":
        return {
            "VERDICTS": json.dumps([
                {"hyp_id": "h0", "plausibility": "leading", "rationale": "best fit"},
                {"hyp_id": "h1", "plausibility": "plausible", "rationale": "possible"},
            ])
        }
    if inp.role == "synth":
        return {"REPORT": "# Debug Report\n\nLeading: h0.\n"}
    return {}


async def test_deep_debug_happy_path(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepDebugWorkflow],
            activities=[write_artifact, emit_finding, _fake],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                DeepDebugWorkflow.run,
                DeepDebugInput(
                    run_id="dd-1",
                    symptom="Test intermittently fails with AssertionError.",
                    reproduction_command="pytest tests/test_thing.py -v",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    num_hypotheses=2,
                    notify=False,
                ),
                id="dd-1",
                task_queue=TASK_QUEUE,
            )
    assert "Report" in result
    assert (tmp_path / "run" / "debug-report.md").exists()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dd-1" in inbox_text
