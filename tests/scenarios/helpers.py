"""Shared helpers and assertion utilities for scenario reliability tests."""

from __future__ import annotations

from pathlib import Path

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE

SANDBOX_RESTRICTIONS = SandboxRestrictions.default.with_passthrough_modules(
    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
)


async def run_scenario_workflow(
    tmp_path: Path,
    workflow_cls,
    workflow_input,
    fake_spawn,
    extra_activities: list | None = None,
    run_id: str = "scenario-test",
) -> str:
    activities = [write_artifact, emit_finding, fake_spawn]
    if extra_activities:
        activities.extend(extra_activities)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[workflow_cls],
            activities=activities,
            workflow_runner=SandboxedWorkflowRunner(restrictions=SANDBOX_RESTRICTIONS),
        ):
            return await env.client.execute_workflow(
                workflow_cls.run,
                workflow_input,
                id=run_id,
                task_queue=TASK_QUEUE,
            )


def assert_no_hidden_failure(tmp_path: Path, expect_inbox: bool = True) -> None:
    """INBOX.md should exist and have content when expected."""
    inbox = tmp_path / "INBOX.md"
    if expect_inbox:
        assert inbox.exists(), f"INBOX.md not found at {inbox}"
        content = inbox.read_text()
        assert len(content.strip()) > 0, "INBOX.md is empty"


def assert_inbox_reflects_outcome(
    tmp_path: Path,
    expected_status: str,
    run_id: str = "",
) -> None:
    """INBOX.md contains the expected status string."""
    inbox = tmp_path / "INBOX.md"
    assert inbox.exists(), f"INBOX.md not found at {inbox}"
    content = inbox.read_text()
    assert expected_status in content, (
        f"Expected '{expected_status}' in INBOX.md, got: {content[:200]}"
    )
    if run_id:
        assert run_id in content, f"run_id '{run_id}' not in INBOX.md"


def assert_report_written(
    tmp_path: Path,
    filename: str = "qa-report.md",
) -> None:
    """An artifact file exists and has real content."""
    report = tmp_path / "run" / filename
    assert report.exists(), f"Report not found at {report}"
    content = report.read_text()
    assert len(content.strip()) > 20, (
        f"Report is too short ({len(content)} bytes): {content[:100]!r}"
    )
