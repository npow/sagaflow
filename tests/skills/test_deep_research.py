"""Workflow-level test for DeepResearchWorkflow."""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.deep_research.workflow import DeepResearchInput, DeepResearchWorkflow


@activity.defn(name="spawn_subagent")
async def _fake(inp: SpawnSubagentInput) -> dict[str, str]:
    if inp.role == "dim-discover":
        return {
            "DIRECTIONS": json.dumps([
                {"id": "d1", "dimension": "HOW", "question": "How does it work?"},
                {"id": "d2", "dimension": "WHO", "question": "Who uses it?"},
            ])
        }
    if inp.role == "researcher":
        return {
            "FINDINGS": "Summary of research.",
            "SOURCES": '["Source A", "Source B"]',
        }
    if inp.role == "synth":
        return {"REPORT": "# Research Report\n\nFindings: ...\n"}
    return {}


async def test_deep_research_happy_path(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[write_artifact, emit_finding, _fake],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                DeepResearchInput(
                    run_id="dr-1",
                    seed="What is Temporal workflow semantics?",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_directions=2,
                    notify=False,
                ),
                id="dr-1",
                task_queue=TASK_QUEUE,
            )
    assert "Report" in result
    assert (tmp_path / "run" / "research-report.md").exists()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dr-1" in inbox_text
