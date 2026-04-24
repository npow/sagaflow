"""Workflow-level test for TeamWorkflow — full v0.3 fidelity."""

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
    role = inp.role
    # Phase 1: plan
    if role == "explore":
        return {"CODEBASE_SUMMARY": "Small Python repo."}
    if role == "planner":
        return {
            "SUBTASKS": json.dumps([
                {"id": "t1", "title": "Implement widget", "description": "Widget impl", "files_likely_touched": []}
            ]),
            "PLAN_SUMMARY": "Build widget.",
        }
    if role == "plan-validator":
        return {"VERDICT": "approved"}
    # Phase 2: PRD
    if role == "analyst":
        return {
            "ACCEPTANCE_CRITERIA": json.dumps([
                {"id": "ac1", "criterion": "Widget exists", "verification_hint": "unit test"}
            ])
        }
    if role == "critic":
        return {"FINDING": ""}
    if role == "falsifiability-judge":
        return {
            "UNFALSIFIABLE_COUNT": "0",
            "AC_VERDICT": "ac1|falsifiable",
            "VERDICT_SUMMARY": "all falsifiable",
        }
    # Phase 3: exec per-worker
    if role.startswith("worker"):
        return {"WORK_SUMMARY": "Implemented the widget.", "FILES_TOUCHED": "[]"}
    if role.startswith("spec-compliance-reviewer"):
        return {"VERDICT": "approved"}
    if role.startswith("code-quality-reviewer"):
        return {"VERDICT": "approved"}
    # Phase 4: verify
    if role.startswith("spec-a-reviewer") or role.startswith("spec-compliance"):
        return {"VERDICT": "approved", "DEFECTS": "[]"}
    if role.startswith("code-b-reviewer") or role.startswith("code-quality"):
        return {"VERDICT": "approved", "DEFECTS": "[]"}
    if role == "verify-judge":
        return {
            "VERDICT": "passed",
            "CRITICAL_COUNT": "0",
            "MAJOR_COUNT": "0",
            "MINOR_COUNT": "0",
        }
    # Phase 5: fix (not exercised by happy path)
    if role.startswith("fix-worker"):
        return {"FIX_SUMMARY": "Fixed."}
    if role.startswith("fix-verifier"):
        return {"FIX_VERDICT": "fixed", "NEW_DEFECT_INTRODUCED": ""}
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
                    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
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
    assert "complete" in result, f"Expected 'complete' in result, got: {result}"
    assert (tmp_path / "run" / "SUMMARY.md").exists()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "team-1" in inbox_text
    assert "DONE" in inbox_text
