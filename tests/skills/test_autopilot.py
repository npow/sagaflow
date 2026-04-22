"""Workflow-level test for AutopilotWorkflow."""

from __future__ import annotations

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.autopilot.workflow import AutopilotInput, AutopilotWorkflow


@activity.defn(name="spawn_subagent")
async def _fake(inp: SpawnSubagentInput) -> dict[str, str]:
    if inp.role == "expand":
        return {"AMBIGUITY_CLASS": "low", "SPEC": "# Spec\nBuild the thing."}
    if inp.role == "plan":
        return {"PLAN": "# Plan\nStep 1.", "ACCEPTANCE_CRITERIA": '["Works"]'}
    if inp.role == "worker":
        return {"WORK_SUMMARY": "Did the slice."}
    if inp.role == "qa":
        return {"AUDIT_LABEL": "clean", "CRITICAL_COUNT": "0"}
    # Any role starting with "judge-"
    if inp.role.startswith("judge-"):
        return {"VERDICT": "approved"}
    return {}


async def test_autopilot_happy_path(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AutopilotWorkflow],
            activities=[write_artifact, emit_finding, _fake],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                AutopilotWorkflow.run,
                AutopilotInput(
                    run_id="ap-1",
                    initial_idea="Build a widget",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    notify=False,
                ),
                id="ap-1",
                task_queue=TASK_QUEUE,
            )
    assert "complete" in result
    assert (tmp_path / "run" / "completion-report.md").exists()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "ap-1" in inbox_text
    assert "DONE" in inbox_text
