"""Tests for the workflow intervention system (pause/resume/inject/takeover/abort).

Covers:
- _sanitize_abort_reason helper
- CLI commands via Click CliRunner + patched async calls
- Workflow-level signal/query integration via Temporal time-skipping env
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from sagaflow.cli import main
from sagaflow.generic.activities import CallClaudeInput, ClaudeResponse
from sagaflow.intervention import AbortRequestedError, InterventionMixin, _sanitize_abort_reason


# ---------------------------------------------------------------------------
# Unit: _sanitize_abort_reason
# ---------------------------------------------------------------------------


class TestSanitizeAbortReason:
    def test_plain_string_passes_through(self) -> None:
        assert _sanitize_abort_reason("user-abort") == "user-abort"

    def test_strips_unsafe_chars(self) -> None:
        assert _sanitize_abort_reason("reason; rm -rf /") == "reason_ rm -rf /"

    def test_truncates_to_200(self) -> None:
        long = "a" * 300
        assert len(_sanitize_abort_reason(long)) == 200

    def test_strips_whitespace(self) -> None:
        assert _sanitize_abort_reason("  padded  ") == "padded"

    def test_empty_string(self) -> None:
        assert _sanitize_abort_reason("") == ""

    def test_allows_safe_punctuation(self) -> None:
        safe = "reason (test), file/path - v1.0"
        assert _sanitize_abort_reason(safe) == safe


# ---------------------------------------------------------------------------
# Unit: AbortRequestedError
# ---------------------------------------------------------------------------


def test_abort_requested_error_is_exception() -> None:
    err = AbortRequestedError("test")
    assert isinstance(err, Exception)
    assert str(err) == "test"


# ---------------------------------------------------------------------------
# CLI: intervention commands via CliRunner
# ---------------------------------------------------------------------------


class TestInterventionCLI:
    """Tests for sagaflow status/pause/resume/inject/takeover/release/abort/conversation."""

    def _mock_handle(self) -> MagicMock:
        handle = MagicMock()
        handle.signal = AsyncMock()
        handle.query = AsyncMock(return_value={"intervention_state": "RUNNING"})
        return handle

    def _patch_connect(self, handle: MagicMock):
        client = MagicMock()
        client.get_workflow_handle = MagicMock(return_value=handle)
        mock_connect = AsyncMock(return_value=client)
        return patch("sagaflow.temporal_client.connect", mock_connect)

    def test_status_command(self) -> None:
        handle = self._mock_handle()
        handle.query = AsyncMock(return_value={
            "intervention_state": "RUNNING",
            "current_phase": "iteration-3",
        })
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["status", "deep-qa-20260427-155146"])
        assert result.exit_code == 0
        assert "RUNNING" in result.output
        handle.query.assert_called_once_with("get_status")

    def test_pause_command(self) -> None:
        handle = self._mock_handle()
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["pause", "deep-qa-20260427-155146"])
        assert result.exit_code == 0
        assert "pause signal sent" in result.output
        handle.signal.assert_called_once_with("pause")

    def test_resume_command(self) -> None:
        handle = self._mock_handle()
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["resume", "deep-qa-20260427-155146"])
        assert result.exit_code == 0
        assert "resume signal sent" in result.output
        handle.signal.assert_called_once_with("resume")

    def test_inject_command(self) -> None:
        handle = self._mock_handle()
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["inject", "my-run", "-m", "focus on security"])
        assert result.exit_code == 0
        assert "message injected" in result.output
        handle.signal.assert_called_once_with("inject", "focus on security")

    def test_takeover_command(self) -> None:
        handle = self._mock_handle()
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["takeover", "my-run"])
        assert result.exit_code == 0
        assert "takeover signal sent" in result.output
        handle.signal.assert_called_once_with("takeover")

    def test_release_command(self) -> None:
        handle = self._mock_handle()
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["release", "my-run"])
        assert result.exit_code == 0
        assert "release signal sent" in result.output
        handle.signal.assert_called_once_with("release")

    def test_abort_command(self) -> None:
        handle = self._mock_handle()
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["abort", "my-run", "--reason", "bad output"])
        assert result.exit_code == 0
        assert "abort signal sent" in result.output
        handle.signal.assert_called_once_with("abort", "bad output")

    def test_abort_default_reason(self) -> None:
        handle = self._mock_handle()
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["abort", "my-run"])
        assert result.exit_code == 0
        handle.signal.assert_called_once_with("abort", "user-abort")

    def test_conversation_command(self) -> None:
        handle = self._mock_handle()
        handle.query = AsyncMock(return_value=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ])
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["conversation", "my-run"])
        assert result.exit_code == 0
        assert "[user] hello" in result.output
        assert "[assistant] world" in result.output
        handle.query.assert_called_once_with("get_conversation")

    def test_conversation_empty(self) -> None:
        handle = self._mock_handle()
        handle.query = AsyncMock(return_value=[])
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["conversation", "my-run"])
        assert result.exit_code == 0
        assert "no messages yet" in result.output

    def test_status_workflow_not_found(self) -> None:
        handle = self._mock_handle()
        handle.query = AsyncMock(side_effect=Exception("workflow not found"))
        runner = CliRunner()
        with self._patch_connect(handle):
            result = runner.invoke(main, ["status", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_run_id_resolution_plain(self) -> None:
        """Plain run-id gets sagaflow- prefix."""
        from sagaflow.cli import _resolve_run_workflow_id
        assert _resolve_run_workflow_id("deep-qa-123") == "sagaflow-deep-qa-123"

    def test_run_id_resolution_already_prefixed(self) -> None:
        """Already-prefixed workflow ID passes through."""
        from sagaflow.cli import _resolve_run_workflow_id
        assert _resolve_run_workflow_id("sagaflow-deep-qa-123") == "sagaflow-deep-qa-123"


# ---------------------------------------------------------------------------
# Workflow integration: InterventionMixin via Temporal time-skipping env
# ---------------------------------------------------------------------------


@pytest.fixture
def _make_workflow_input(tmp_path: Path):
    """Factory for ClaudeSkillInput with a valid run_dir."""
    from sagaflow.generic.workflow import ClaudeSkillInput

    def _make(**overrides) -> ClaudeSkillInput:
        run_dir = tmp_path / "runs" / "intervention-test"
        run_dir.mkdir(parents=True, exist_ok=True)
        defaults = dict(
            run_id="intervention-test",
            run_dir=str(run_dir),
            inbox_path=str(tmp_path / "INBOX.md"),
            skill_name="test-skill",
            skill_md_content="# Test\nDo the thing.",
            user_args={},
            max_iterations=10,
            tier_name="SONNET",
            notify=False,
        )
        defaults.update(overrides)
        return ClaudeSkillInput(**defaults)

    return _make


async def _run_with_signals(
    env,
    workflow_input,
    *,
    activities: list,
    signal_fn=None,
) -> str:
    """Run ClaudeSkillWorkflow with optional signal injection mid-flight."""
    from temporalio.worker import Worker as TWorker
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

    from sagaflow.generic.workflow import ClaudeSkillWorkflow, SubagentWorkflow
    from sagaflow.slack_progress import report_slack_state_change
    from sagaflow.temporal_client import TASK_QUEUE

    async with TWorker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
        activities=activities + [report_slack_state_change],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
            )
        ),
    ):
        handle = await env.client.start_workflow(
            ClaudeSkillWorkflow.run,
            workflow_input,
            id="sagaflow-intervention-test",
            task_queue=TASK_QUEUE,
        )
        if signal_fn:
            await signal_fn(handle)
        return await handle.result()


class TestWorkflowAbortSignal:
    """Verify the abort signal terminates the workflow."""

    @pytest.mark.asyncio
    async def test_abort_stops_workflow(self, tmp_path, _make_workflow_input) -> None:
        from temporalio import activity
        from temporalio.testing import WorkflowEnvironment

        from sagaflow.durable.activities import emit_finding, write_artifact
        from sagaflow.generic.activities import (
            generic_tool_adapter_bash_tool,
            generic_tool_adapter_glob_tool,
            generic_tool_adapter_grep_tool,
            generic_tool_adapter_read_file_tool,
            generic_tool_adapter_write_artifact,
        )

        from sagaflow.generic.activities import ClaudeToolUse

        activity_entered = asyncio.Event()
        call_count = 0

        @activity.defn(name="call_claude_with_tools")
        async def slow_claude(inp: CallClaudeInput) -> ClaudeResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                activity_entered.set()
                await asyncio.sleep(0.5)
                return ClaudeResponse(
                    text=f"turn-{call_count}",
                    tool_uses=[ClaudeToolUse(id="t1", name="read_file", input={"path": "/dev/null"})],
                    stop_reason="tool_use",
                )
            return ClaudeResponse(text=f"turn-{call_count}", tool_uses=[], stop_reason="end_turn")

        inp = _make_workflow_input(max_iterations=50)

        async def send_abort(handle):
            await activity_entered.wait()
            await handle.signal("abort", "testing abort")

        async with await WorkflowEnvironment.start_time_skipping() as env:
            result = await _run_with_signals(
                env,
                inp,
                activities=[
                    slow_claude,
                    write_artifact,
                    emit_finding,
                    generic_tool_adapter_read_file_tool,
                    generic_tool_adapter_bash_tool,
                    generic_tool_adapter_grep_tool,
                    generic_tool_adapter_glob_tool,
                    generic_tool_adapter_write_artifact,
                ],
                signal_fn=send_abort,
            )

        assert "Aborted by operator" in result


class TestWorkflowStatusQuery:
    """Verify the get_status query returns intervention state."""

    @pytest.mark.asyncio
    async def test_query_returns_state(self, tmp_path, _make_workflow_input) -> None:
        from temporalio import activity
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker as TWorker
        from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

        from sagaflow.durable.activities import emit_finding, write_artifact
        from sagaflow.generic.activities import (
            CallClaudeInput,
            ClaudeResponse,
            generic_tool_adapter_bash_tool,
            generic_tool_adapter_glob_tool,
            generic_tool_adapter_grep_tool,
            generic_tool_adapter_read_file_tool,
            generic_tool_adapter_write_artifact,
        )
        from sagaflow.generic.workflow import ClaudeSkillWorkflow, SubagentWorkflow
        from sagaflow.slack_progress import report_slack_state_change
        from sagaflow.temporal_client import TASK_QUEUE

        @activity.defn(name="call_claude_with_tools")
        async def quick_claude(inp: CallClaudeInput) -> ClaudeResponse:
            return ClaudeResponse(text="done", tool_uses=[], stop_reason="end_turn")

        inp = _make_workflow_input()

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with TWorker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
                activities=[
                    quick_claude, write_artifact, emit_finding,
                    generic_tool_adapter_read_file_tool,
                    generic_tool_adapter_bash_tool,
                    generic_tool_adapter_grep_tool,
                    generic_tool_adapter_glob_tool,
                    generic_tool_adapter_write_artifact,
                    report_slack_state_change,
                ],
                workflow_runner=SandboxedWorkflowRunner(
                    restrictions=SandboxRestrictions.default.with_passthrough_modules(
                        "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
                    )
                ),
            ):
                handle = await env.client.start_workflow(
                    ClaudeSkillWorkflow.run,
                    inp,
                    id="sagaflow-query-test",
                    task_queue=TASK_QUEUE,
                )
                status = await handle.query("get_status")
                assert status["skill_name"] == "test-skill"
                assert status["intervention_state"] == "RUNNING"
                await handle.result()
