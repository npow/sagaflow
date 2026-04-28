"""Scenario reliability tests for AutopilotWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.

Note: Autopilot delegates to 4 child workflows (DeepPlan, Team, DeepQa,
LoopUntilDone), so the Worker must register all of them. The base fake
handles all roles across the union of these workflows.

int() trap: BLOCKING_SCENARIO_COUNT must be numeric string.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.autopilot.workflow import AutopilotInput, AutopilotWorkflow
from skills.deep_plan.workflow import DeepPlanWorkflow
from skills.deep_qa.activities import read_text_file
from skills.deep_qa.workflow import DeepQaWorkflow
from skills.loop_until_done.workflow import LoopUntilDoneWorkflow
from skills.team.workflow import TeamWorkflow

from tests.scenarios.helpers import (
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake covering all autopilot + child workflow roles
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _ap_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role

    # ── Autopilot direct spawns ──
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
        return {"VERDICT": "approved", "BLOCKING_SCENARIO_COUNT": "0", "DIMENSION": "correctness"}

    # ── DeepPlan roles ──
    if role == "planner":
        return {
            "PLAN": "# Plan\nDo X.",
            "ACCEPTANCE_CRITERIA": json.dumps([{"id": "c1", "criterion": "Works"}]),
        }
    if role == "architect":
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "adr":
        return {"ADR": "# ADR\n\nDecision."}

    # ── Team roles ──
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

    # ── DeepQa roles ──
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

    # ── LoopUntilDone roles ──
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

    # ── Generic fallback ──
    if role == "critic":
        return {"VERDICT": "APPROVE"}

    return {}


def _make_input(tmp_path, run_id: str) -> AutopilotInput:
    return AutopilotInput(
        run_id=run_id,
        initial_idea="Build a widget",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        notify=False,
        hard_cap_usd=25.0,
        max_revalidation_rounds=2,
    )


_CHILD_WORKFLOWS = [DeepPlanWorkflow, TeamWorkflow, DeepQaWorkflow, LoopUntilDoneWorkflow]


async def _run_ap(tmp_path, fake_spawn, run_id: str) -> str:
    inp = _make_input(tmp_path, run_id)
    return await run_scenario_workflow(
        tmp_path,
        AutopilotWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress, read_text_file],
        extra_workflows=_CHILD_WORKFLOWS,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: spec-writer returns malformed output
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="malformed spec-writer shouldn't crash judge pipeline",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_spec_writer_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["spec-writer"])
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-spec-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: ambiguity-classifier gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="model error on classifier shouldn't prevent routing",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_ambiguity_classifier_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["ambiguity-classifier"])
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-classifier-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: planner returns truncated (child DeepPlan role)
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="truncated planner in child workflow shouldn't crash autopilot",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_planner_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["planner"])
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-planner-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: analyst returns empty (child Team role)
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="empty analyst in child workflow shouldn't crash autopilot",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_analyst_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["analyst"])
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-analyst-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: autopilot-direct roles all malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="all autopilot-direct roles malformed shouldn't crash child dispatch",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_autopilot_direct_malformed(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["ambiguity-classifier", "spec-writer"],
    )
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-direct-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across autopilot and child roles
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="heterogeneous failures across autopilot + child roles",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["spec-writer"],
        empty_roles=["explore"],
        garbage_input_roles=["critic"],
    )
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage spec-writer response
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="garbage spec-writer output shouldn't crash spec validation",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_spec_writer(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["spec-writer"])
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-garbage-spec")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: ambiguity-classifier returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="autopilot",
    traces_bug="empty classifier should still attempt spec writing",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_ambiguity_classifier_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["ambiguity-classifier"])
    fake = make_scenario_fake(_ap_base_fake, config)
    result = await _run_ap(tmp_path, fake, "ap-sc-classifier-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
