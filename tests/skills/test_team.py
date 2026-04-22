"""Workflow-level test for TeamWorkflow."""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.team.workflow import TeamInput, TeamWorkflow


@activity.defn(name="spawn_subagent")
async def _fake(inp: SpawnSubagentInput) -> dict[str, str]:
    if inp.role == "planner":
        return {"SUBTASKS": json.dumps([{"id": "t1", "title": "Do it", "description": "Work"}])}
    if inp.role == "analyst":
        return {"ACCEPTANCE_CRITERIA": json.dumps([{"id": "c1", "criterion": "Done", "verification_hint": ""}])}
    if inp.role == "worker":
        return {"WORK_SUMMARY": "Implemented.", "FILES_TOUCHED": "[]"}
    if inp.role == "verifier":
        return {"VERDICT": "passed", "DEFECTS": "[]"}
    return {}


async def test_team_workflow_happy_path(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[TeamWorkflow],
            activities=[write_artifact, emit_finding, _fake],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                TeamWorkflow.run,
                TeamInput(
                    run_id="team-1",
                    task="Build a widget",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    n_workers=1,
                    max_fix_iters=1,
                    notify=False,
                ),
                id="team-1",
                task_queue=TASK_QUEUE,
            )
    assert "complete" in result
    assert (tmp_path / "run" / "SUMMARY.md").exists()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "team-1" in inbox_text
    assert "DONE" in inbox_text
