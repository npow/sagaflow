"""Workflow-level test for DeepQaWorkflow using time-skipped Temporal env.

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
from skills.deep_qa.activities import read_text_file
from skills.deep_qa.workflow import DeepQaInput, DeepQaWorkflow


@activity.defn(name="spawn_subagent")
async def _fake_spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Does it handle empty input?"},
                {"id": "a2", "dimension": "clarity", "question": "Is the API name self-explanatory?"},
            ])
        }
    if role == "critic":
        return {
            "DEFECTS": json.dumps([
                {
                    "id": f"d-{role}",
                    "title": "Empty-input path is undefined",
                    "severity": "major",
                    "dimension": "correctness",
                    "scenario": "Called with no arguments.",
                    "root_cause": "No input guard.",
                }
            ])
        }
    if role == "synth":
        return {"REPORT": "# QA Report (fake)\n\n1 major defect.\n"}
    return {}


async def test_deep_qa_roundtrip_produces_report(tmp_path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("Hello world.\nThis is the artifact under QA.\n")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepQaWorkflow],
            activities=[write_artifact, emit_finding, _fake_spawn_subagent, read_text_file],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                DeepQaWorkflow.run,
                DeepQaInput(
                    run_id="dq-1",
                    artifact_path=str(artifact),
                    artifact_type="doc",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_rounds=1,
                    notify=False,
                ),
                id="dq-1",
                task_queue=TASK_QUEUE,
            )

    # Result summary mentions severity tally + the report path.
    assert "defects across" in result
    report_path = tmp_path / "run" / "qa-report.md"
    assert report_path.exists()
    assert "QA Report" in report_path.read_text()
    # INBOX got a DONE entry.
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dq-1" in inbox_text
    assert "DONE" in inbox_text
