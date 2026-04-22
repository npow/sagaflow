"""Workflow-level test for FlakyTestWorkflow using time-skipped Temporal env.

The test swaps `spawn_subagent` and `run_test_subprocess` with fake activities
that produce deterministic, role-dispatched outputs. Keeps the test hermetic —
no real subprocess or Anthropic/claude-p calls.
"""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.flaky_test_diagnoser.workflow import FlakyTestInput, FlakyTestWorkflow


# ── fake activities ────────────────────────────────────────────────────────────

_run_counter: list[int] = []  # module-level to allow the fake to alternate pass/fail


@activity.defn(name="run_test_subprocess")
async def _fake_run_test_subprocess(command: str, timeout: int = 60) -> dict[str, int]:
    """Alternate pass/fail to simulate ~50% flakiness."""
    call_index = len(_run_counter)
    _run_counter.append(call_index)
    exit_code = 1 if call_index % 2 == 1 else 0  # 0=pass on even, 1=fail on odd
    return {"exit_code": exit_code, "duration_ms": 42}


@activity.defn(name="spawn_subagent")
async def _fake_spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "hypothesis-gen":
        return {
            "HYPOTHESES": json.dumps([
                {
                    "id": "h1",
                    "category": "TIMING",
                    "mechanism": "Race condition between test setup and async background job",
                    "uncertainty": "high",
                },
                {
                    "id": "h2",
                    "category": "SHARED_STATE",
                    "mechanism": "Global singleton not reset between test runs",
                    "uncertainty": "medium",
                },
                {
                    "id": "h3",
                    "category": "ORDERING",
                    "mechanism": "Test depends on alphabetical execution order of test files",
                    "uncertainty": "low",
                },
            ])
        }
    if role == "judge":
        return {
            "RANKINGS": json.dumps([
                {"hyp_id": "h2", "rank": 1, "uncertainty": "medium"},
                {"hyp_id": "h1", "rank": 2, "uncertainty": "high"},
                {"hyp_id": "h3", "rank": 3, "uncertainty": "low"},
            ])
        }
    if role == "synth":
        return {
            "REPORT": (
                "# Flaky Test Diagnosis\n\n"
                "**Test:** tests/test_example.py::test_flaky\n"
                "**Fail rate:** 50%\n\n"
                "## Top Hypotheses\n\n"
                "1. SHARED_STATE — Global singleton not reset between runs\n"
                "2. TIMING — Race condition with async background job\n\n"
                "## Recommendation\n\n"
                "Add teardown to reset the global singleton after each run.\n"
            ),
            "TERMINATION_LABEL": "narrowed_to_N_hypotheses",
        }
    return {}


# ── test ──────────────────────────────────────────────────────────────────────


async def test_flaky_test_diagnoser_roundtrip(tmp_path) -> None:
    # Reset global counter so the test is idempotent if run multiple times.
    _run_counter.clear()

    run_dir = tmp_path / "run"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[FlakyTestWorkflow],
            activities=[
                write_artifact,
                emit_finding,
                _fake_spawn_subagent,
                _fake_run_test_subprocess,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                FlakyTestWorkflow.run,
                FlakyTestInput(
                    run_id="ftd-test-1",
                    test_identifier="tests/test_example.py::test_flaky",
                    run_dir=str(run_dir),
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_command="true",  # dummy command; fake activity intercepts it
                    n_runs=4,  # small sample for test speed
                    notify=False,
                ),
                id="ftd-test-1",
                task_queue=TASK_QUEUE,
            )

    # Result string mentions fail_rate and report path.
    assert "fail_rate=" in result
    assert "Report:" in result

    # report.md was written to run_dir.
    report_path = run_dir / "report.md"
    assert report_path.exists(), f"report.md not found at {report_path}"
    report_text = report_path.read_text()
    assert "Flaky Test Diagnosis" in report_text

    # INBOX got an entry with the run_id.
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "ftd-test-1" in inbox_text

    # 4 subprocess calls were made (n_runs=4).
    assert len(_run_counter) == 4


async def test_flaky_test_diagnoser_not_reproduced(tmp_path) -> None:
    """When all runs pass, workflow should short-circuit with not_reproduced."""

    @activity.defn(name="run_test_subprocess")
    async def _always_pass(command: str, timeout: int = 60) -> dict[str, int]:
        return {"exit_code": 0, "duration_ms": 10}

    run_dir = tmp_path / "run"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[FlakyTestWorkflow],
            activities=[
                write_artifact,
                emit_finding,
                _fake_spawn_subagent,
                _always_pass,
            ],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                FlakyTestWorkflow.run,
                FlakyTestInput(
                    run_id="ftd-test-2",
                    test_identifier="tests/test_stable.py::test_stable",
                    run_dir=str(run_dir),
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_command="true",
                    n_runs=4,
                    notify=False,
                ),
                id="ftd-test-2",
                task_queue=TASK_QUEUE,
            )

    assert "not reproduced" in result.lower()
    report_path = run_dir / "report.md"
    assert report_path.exists()
    assert "Not Reproduced" in report_path.read_text()

    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "ftd-test-2" in inbox_text
    assert "not_reproduced" in inbox_text
