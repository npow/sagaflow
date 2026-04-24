"""Workflow-level test for ProposalReviewWorkflow using time-skipped Temporal env.

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
from skills.proposal_reviewer.workflow import ProposalReviewInput, ProposalReviewWorkflow


@activity.defn(name="spawn_subagent")
async def _fake_spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "claim-extract":
        return {
            "CLAIMS": json.dumps([
                {"id": "c1", "text": "This product will capture 30% market share in year 1.", "tier": "core"},
                {"id": "c2", "text": "The technology is patentable and defensible.", "tier": "supporting"},
            ])
        }
    if role == "critic":
        return {
            "WEAKNESSES": json.dumps([
                {
                    "id": f"w-{inp.role}",
                    "title": "Market share claim lacks evidence",
                    "severity": "high",
                    "dimension": "evidence",
                    "scenario": "No comparable market data is cited.",
                }
            ])
        }
    if role == "fact-check":
        return {
            "VERDICT": "PARTIALLY_TRUE",
            "CONFIDENCE": "medium",
        }
    if role == "synth":
        return {"REPORT": "# Proposal Review (fake)\n\nMixed evidence found.\n"}
    return {}


async def test_proposal_reviewer_roundtrip_produces_report(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ProposalReviewWorkflow],
            activities=[write_artifact, emit_finding, _fake_spawn_subagent],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                ProposalReviewWorkflow.run,
                ProposalReviewInput(
                    run_id="pr-1",
                    proposal_text="This is a test proposal.",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    notify=False,
                ),
                id="pr-1",
                task_queue=TASK_QUEUE,
            )

    # Result summary mentions claims, weaknesses, label.
    assert "claims extracted" in result
    assert "weaknesses found" in result

    # review.md was written.
    report_path = tmp_path / "run" / "review.md"
    assert report_path.exists()
    assert "Proposal Review" in report_path.read_text()

    # INBOX got an entry with run_id.
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "pr-1" in inbox_text
    assert "DONE" in inbox_text


async def test_proposal_reviewer_four_critics_all_called(tmp_path) -> None:
    """Verify that all 4 critic dimensions are dispatched."""
    critic_dimensions: list[str] = []

    @activity.defn(name="spawn_subagent")
    async def _tracking_fake(inp: SpawnSubagentInput) -> dict[str, str]:
        if inp.role == "claim-extract":
            return {
                "CLAIMS": json.dumps([
                    {"id": "c1", "text": "Claim one.", "tier": "core"},
                ])
            }
        if inp.role == "critic":
            # Extract dimension from system prompt (contains the dimension name).
            for dim in ("viability", "competition", "structural", "evidence"):
                if dim in inp.system_prompt:
                    critic_dimensions.append(dim)
                    break
            return {"WEAKNESSES": "[]"}
        if inp.role == "fact-check":
            return {"VERDICT": "VERIFIED", "CONFIDENCE": "high"}
        if inp.role == "synth":
            return {"REPORT": "# Review\n\nAll good.\n"}
        return {}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ProposalReviewWorkflow],
            activities=[write_artifact, emit_finding, _tracking_fake],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
                )
            ),
        ):
            await env.client.execute_workflow(
                ProposalReviewWorkflow.run,
                ProposalReviewInput(
                    run_id="pr-2",
                    proposal_text="Test proposal text.",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run2"),
                    notify=False,
                ),
                id="pr-2",
                task_queue=TASK_QUEUE,
            )

    assert set(critic_dimensions) == {"viability", "competition", "structural", "evidence"}


async def test_quorum_failure_with_malformed_sentinels_writes_substantive_report(tmp_path) -> None:
    """When all 4 critics return malformed sentinels, quorum fails but review.md
    must contain a substantive body — not just headers."""

    @activity.defn(name="spawn_subagent")
    async def _malformed_fake(inp: SpawnSubagentInput) -> dict[str, str]:
        if inp.role == "claim-extractor":
            return {
                "CLAIMS": json.dumps([
                    {"id": "c1", "text": "Revenue will triple.", "tier": "core"},
                ])
            }
        if inp.role == "critic":
            # Simulate the malformed sentinel that upstream now returns.
            return {
                "_sagaflow_malformed": "1",
                "_error": "structured output parse failed",
                "_raw": "The proposal has significant gaps in market analysis.",
            }
        return {}

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ProposalReviewWorkflow],
            activities=[write_artifact, emit_finding, _malformed_fake],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                ProposalReviewWorkflow.run,
                ProposalReviewInput(
                    run_id="pr-quorum",
                    proposal_text="Triple revenue proposal.",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run-q"),
                    notify=False,
                ),
                id="pr-quorum",
                task_queue=TASK_QUEUE,
            )

    assert "critic quorum failed" in result

    report_path = tmp_path / "run-q" / "review.md"
    assert report_path.exists()
    report_text = report_path.read_text()

    # Report must NOT be empty — it must explain the situation.
    assert len(report_text) > 200, f"report too short ({len(report_text)} bytes): {report_text!r}"
    assert "Early termination" in report_text
    assert "critic quorum failed" in report_text
    # Raw critic findings must be dumped.
    assert "Raw Critic Findings" in report_text
    assert "significant gaps in market analysis" in report_text
    # Claims extracted before quorum failure should still appear.
    assert "Revenue will triple" in report_text
