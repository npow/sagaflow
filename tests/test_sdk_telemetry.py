"""Tests for the record_sdk_telemetry activity."""

from __future__ import annotations

from dataclasses import asdict


def test_sdk_telemetry_input_dataclass():
    from sagaflow.durable.activities import SdkTelemetryInput

    inp = SdkTelemetryInput(
        role="critic",
        tier="HAIKU",
        system_prompt="you are a critic",
        user_prompt="review this",
        run_dir="/tmp/test-run",
        step_index=0,
        model="anthropic:claude-haiku-4-5-20251001",
        input_tokens=100,
        output_tokens=50,
        duration_seconds=1.5,
    )
    assert inp.role == "critic"
    assert inp.input_tokens == 100
    d = asdict(inp)
    assert d["tier"] == "HAIKU"


def test_budget_check_input_dataclass():
    from sagaflow.durable.activities import BudgetCheckInput, BudgetCheckResult

    inp = BudgetCheckInput(workflow_id="test/run-1", role="critic", tier="HAIKU")
    assert inp.workflow_id == "test/run-1"

    result = BudgetCheckResult(effective_tier="HAIKU", abort=False)
    assert not result.abort
    assert result.effective_tier == "HAIKU"
