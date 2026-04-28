"""Scenario reliability tests for DeepPlanWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.
"""

from __future__ import annotations

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.deep_plan.workflow import DeepPlanInput, DeepPlanWorkflow

from tests.scenarios.helpers import (
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for deep-plan
# ---------------------------------------------------------------------------

_PLAN = "# Plan\n\nStep 1: Implement core module.\nStep 2: Add tests.\nStep 3: Deploy.\n"
_CRITERIA = '["AC-001: all tests pass", "AC-002: canary succeeds"]'


@activity.defn(name="spawn_subagent")
async def _dp_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "planner":
        return {"PLAN": _PLAN, "ACCEPTANCE_CRITERIA": _CRITERIA}
    if role == "architect":
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "critic":
        return {"VERDICT": "APPROVE"}
    if role == "adr":
        return {"ADR": "# ADR\n\n## Status\nAccepted\n\n## Decision\nProceed with plan.\n"}
    return {}


def _make_input(tmp_path, run_id: str, max_iter: int = 1) -> DeepPlanInput:
    return DeepPlanInput(
        task="Build a distributed rate-limiter service.",
        run_id=run_id,
        run_dir=str(tmp_path / "run"),
        inbox_path=str(tmp_path / "INBOX.md"),
        max_iter=max_iter,
        notify=False,
    )


async def _run_dp(tmp_path, fake_spawn, run_id: str, max_iter: int = 1) -> str:
    inp = _make_input(tmp_path, run_id, max_iter)
    return await run_scenario_workflow(
        tmp_path,
        DeepPlanWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: planner returns malformed output
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="malformed planner output should terminate with clear label",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_planner_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["planner"])
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-planner-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: architect gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="model error on architect shouldn't lose the plan",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_architect_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["architect"])
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-architect-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: critic returns truncated response
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="truncated critic verdict shouldn't block plan finalization",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_critic_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["critic"])
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-critic-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: ADR returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="empty ADR should still finalize the plan",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_adr_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["adr"])
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-adr-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: all roles return malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="total malformed cascade shouldn't crash — should degrade gracefully",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_all_roles_malformed(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["planner", "architect", "critic", "adr"],
    )
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-all-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="heterogeneous failures across plan roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["architect"],
        empty_roles=["critic"],
        garbage_input_roles=["adr"],
    )
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: planner returns empty — no plan to review
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="empty planner output should terminate with clear label",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_planner_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["planner"])
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-planner-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: garbage architect response
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-plan",
    traces_bug="garbage architect output shouldn't crash verdict parsing",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_architect(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["architect"])
    fake = make_scenario_fake(_dp_base_fake, config)
    result = await _run_dp(tmp_path, fake, "dp-sc-garbage-architect")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
