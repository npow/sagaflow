"""Workflow-level test for AutopilotWorkflow (v0.3 full fidelity).

The autopilot workflow delegates to 4 child workflows (DeepPlan, Team, DeepQa,
LoopUntilDone), so the test Worker must register ALL of them along with their
activities + a role-dispatching fake spawn_subagent that handles every role
the union of these workflows spawns.
"""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.autopilot.workflow import AutopilotInput, AutopilotWorkflow
from skills.deep_plan.workflow import DeepPlanWorkflow
from skills.deep_qa.activities import read_text_file
from skills.deep_qa.workflow import DeepQaWorkflow
from skills.loop_until_done.workflow import LoopUntilDoneWorkflow
from skills.team.workflow import TeamWorkflow


@activity.defn(name="spawn_subagent")
async def _fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role

    # Autopilot direct spawns
    if role == "ambiguity-classifier":
        return {
            "AMBIGUITY_SCORE": "0.2",
            "AMBIGUITY_CLASS": "low",
            "CONCRETE_ANCHORS": "3",
            "ROUTED_TO": "spec",
        }
    if role == "spec-writer":
        return {"SPEC": "# Spec\n\nBuild the thing."}
    if role.startswith("judge-"):
        # 3 dims × N rounds — always approved with 0 blocking scenarios.
        return {"VERDICT": "approved", "BLOCKING_SCENARIO_COUNT": "0", "DIMENSION": "correctness"}

    # DeepPlan roles
    if role == "planner":
        return {
            "PLAN": "# Plan\nDo X.",
            "ACCEPTANCE_CRITERIA": json.dumps([{"id": "c1", "criterion": "Works"}]),
        }
    if role == "architect":
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "critic":
        return {"VERDICT": "APPROVE"}
    if role == "adr":
        return {"ADR": "# ADR\n\nDecision."}

    # Team roles (happy path)
    if role == "explore":
        return {"CODEBASE_SUMMARY": "Small repo."}
    if role == "plan-validator":
        return {"VERDICT": "approved"}
    if role == "analyst":
        return {
            "ACCEPTANCE_CRITERIA": json.dumps([
                {"id": "ac1", "criterion": "ok", "verification_hint": ""}
            ])
        }
    if role == "falsifiability-judge":
        return {"UNFALSIFIABLE_COUNT": "0", "AC_VERDICT": "ac1|falsifiable"}
    if role.startswith("worker"):
        return {"WORK_SUMMARY": "Done.", "FILES_TOUCHED": "[]"}
    if role.startswith("spec-compliance-reviewer") or role.startswith("code-quality-reviewer"):
        return {"VERDICT": "approved"}
    if role == "verify-judge":
        return {"VERDICT": "passed", "CRITICAL_COUNT": "0", "MAJOR_COUNT": "0", "MINOR_COUNT": "0"}

    # DeepQa roles
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Does it work?"}
            ])
        }
    if role in ("judge-pass-1", "judge-pass-2"):
        return {
            "VERDICTS": json.dumps([
                {"defect_id": "d1", "severity": "minor", "confidence": "high",
                 "calibration": "confirm", "rationale": "ok"}
            ])
        }
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "all carried"}
    if role == "synth":
        return {"REPORT": "# QA Report\nNo defects."}

    # Loop-until-done roles (in case qa-fix fires, though it shouldn't on clean QA)
    if role == "prd":
        return {
            "STORIES": json.dumps([
                {"id": "s1", "title": "x", "criteria": [
                    {"id": "ac1", "criterion": "done", "verification_command": "true",
                     "expected_pattern": ".*"}
                ]}
            ])
        }
    if role == "falsifiability":
        return {"CRITERION_VERDICTS": json.dumps([{"criterion_id": "ac1", "pass": True}])}
    if role == "executor":
        return {"WORK_DESCRIPTION": "done"}
    if role == "verifier":
        return {"VERIFIED": "true"}
    if role == "reviewer":
        return {"OVERALL_VERDICT": "all_stories_passed"}

    # Critics for generic roles not above
    if role == "critic":
        return {"VERDICT": "APPROVE"}

    return {}


_SANDBOX = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "httpx", "anthropic", "sagaflow", "skills"
    )
)


async def test_autopilot_happy_path(tmp_path) -> None:
    """Happy path: every phase advances, all judges approve → complete."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[
                AutopilotWorkflow,
                DeepPlanWorkflow,
                TeamWorkflow,
                DeepQaWorkflow,
                LoopUntilDoneWorkflow,
            ],
            activities=[write_artifact, emit_finding, _fake, read_text_file],
            workflow_runner=_SANDBOX,
        ):
            result = await env.client.execute_workflow(
                AutopilotWorkflow.run,
                AutopilotInput(
                    run_id="ap-1",
                    initial_idea="Build a widget",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    notify=False,
                    hard_cap_usd=25.0,
                    max_revalidation_rounds=2,
                ),
                id="ap-1",
                task_queue=TASK_QUEUE,
            )
    assert "complete" in result
    assert (tmp_path / "run" / "completion-report.md").exists()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "ap-1" in inbox_text
    assert "DONE" in inbox_text


async def test_autopilot_budget_exhausted(tmp_path) -> None:
    """Tiny budget → exhausts before completing → budget_exhausted."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[
                AutopilotWorkflow,
                DeepPlanWorkflow,
                TeamWorkflow,
                DeepQaWorkflow,
                LoopUntilDoneWorkflow,
            ],
            activities=[write_artifact, emit_finding, _fake, read_text_file],
            workflow_runner=_SANDBOX,
        ):
            result = await env.client.execute_workflow(
                AutopilotWorkflow.run,
                AutopilotInput(
                    run_id="ap-budget",
                    initial_idea="Build a widget",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run-budget"),
                    notify=False,
                    hard_cap_usd=0.25,   # only enough for expand phase
                    max_revalidation_rounds=1,
                ),
                id="ap-budget",
                task_queue=TASK_QUEUE,
            )
    # Completion report still written; termination label must be budget_exhausted
    # or blocked at an early phase (depends on which activity first runs out).
    assert "budget_exhausted" in result or "blocked_at_phase_" in result
    assert (tmp_path / "run-budget" / "completion-report.md").exists()
