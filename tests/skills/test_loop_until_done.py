"""Workflow-level test for LoopUntilDoneWorkflow using time-skipped Temporal env.

The test swaps `spawn_subagent` with a role-dispatching fake that returns
deterministic structured outputs per role. Happy path: 1 story with 1 criterion,
falsifiability pass, executor completes, verifier passes, reviewer says all_stories_passed.
"""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.loop_until_done.workflow import LoopUntilDoneInput, LoopUntilDoneWorkflow


@activity.defn(name="spawn_subagent")
async def _fake_spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role

    if role == "prd":
        return {
            "STORIES": json.dumps([
                {
                    "id": "s1",
                    "title": "Implement hello endpoint",
                    "criteria": [
                        {
                            "id": "c1",
                            "criterion": "GET /hello returns 200",
                            "verification_command": "curl -s http://localhost:8080/hello",
                            "expected_pattern": "200",
                        }
                    ],
                }
            ])
        }

    if role == "falsifiability":
        return {
            "CRITERION_VERDICTS": json.dumps([
                {"criterion_id": "c1", "pass": True, "rationale": "Observable HTTP status code."}
            ])
        }

    if role == "executor":
        return {"WORK_DESCRIPTION": "Created GET /hello endpoint returning HTTP 200 with body 'Hello'."}

    if role == "verifier":
        return {"VERIFIED": "true"}

    if role == "reviewer":
        return {"OVERALL_VERDICT": "all_stories_passed"}

    return {}


async def test_loop_until_done_happy_path(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[LoopUntilDoneWorkflow],
            activities=[write_artifact, emit_finding, _fake_spawn_subagent],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                LoopUntilDoneWorkflow.run,
                LoopUntilDoneInput(
                    run_id="lud-1",
                    task="Build a hello endpoint",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_iter=5,
                    notify=False,
                ),
                id="lud-1",
                task_queue=TASK_QUEUE,
            )

    # Reviewer returned all_stories_passed.
    assert result == "all_stories_passed"

    # summary.md was written to run_dir.
    summary_path = tmp_path / "run" / "summary.md"
    assert summary_path.exists(), f"summary.md not found at {summary_path}"
    summary_text = summary_path.read_text()
    assert "loop-until-done" in summary_text
    assert "all_stories_passed" in summary_text

    # INBOX got an entry for this run_id.
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "lud-1" in inbox_text
    assert "all_stories_passed" in inbox_text
