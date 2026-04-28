"""Scenario reliability tests for TeamWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.

Note: int() traps — UNFALSIFIABLE_COUNT, CRITICAL_COUNT, MAJOR_COUNT,
MINOR_COUNT must all be numeric strings in the base fake.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.team.workflow import TeamInput, TeamWorkflow

from tests.scenarios.helpers import (
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for team
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _team_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "explore":
        return {"CODEBASE_SUMMARY": "Small Python repo."}
    if role == "planner":
        return {
            "SUBTASKS": json.dumps([
                {"id": "t1", "title": "Implement widget", "description": "Widget impl",
                 "files_likely_touched": []}
            ]),
            "PLAN_SUMMARY": "Build widget.",
        }
    if role == "plan-validator":
        return {"VERDICT": "approved"}
    if role == "analyst":
        return {
            "ACCEPTANCE_CRITERIA": json.dumps([
                {"id": "ac1", "criterion": "Widget exists", "verification_hint": "unit test"}
            ])
        }
    if role == "critic":
        return {"FINDING": ""}
    if role == "falsifiability-judge":
        return {
            "UNFALSIFIABLE_COUNT": "0",
            "AC_VERDICT": "ac1|falsifiable",
            "VERDICT_SUMMARY": "all falsifiable",
        }
    if role.startswith("worker"):
        return {"WORK_SUMMARY": "Implemented the widget.", "FILES_TOUCHED": "[]"}
    if role.startswith("spec-compliance-reviewer"):
        return {"VERDICT": "approved"}
    if role.startswith("code-quality-reviewer"):
        return {"VERDICT": "approved"}
    if role.startswith("spec-a-reviewer") or role.startswith("spec-compliance"):
        return {"VERDICT": "approved", "DEFECTS": "[]"}
    if role.startswith("code-b-reviewer") or role.startswith("code-quality"):
        return {"VERDICT": "approved", "DEFECTS": "[]"}
    if role == "verify-judge":
        return {
            "VERDICT": "passed",
            "CRITICAL_COUNT": "0",
            "MAJOR_COUNT": "0",
            "MINOR_COUNT": "0",
        }
    if role.startswith("fix-worker"):
        return {"FIX_SUMMARY": "Fixed."}
    if role.startswith("fix-verifier"):
        return {"FIX_VERDICT": "fixed", "NEW_DEFECT_INTRODUCED": ""}
    return {}


def _make_input(tmp_path, run_id: str) -> TeamInput:
    return TeamInput(
        run_id=run_id,
        task="Build a widget",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        n_workers=1,
        max_fix_iters=1,
        notify=False,
    )


async def _run_team(tmp_path, fake_spawn, run_id: str) -> str:
    inp = _make_input(tmp_path, run_id)
    return await run_scenario_workflow(
        tmp_path,
        TeamWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: planner returns malformed output
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="malformed planner shouldn't crash task decomposition",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_planner_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["planner"])
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-planner-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: verify-judge gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="model error on verify-judge shouldn't lose review results",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_verify_judge_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["verify-judge"])
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-judge-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: analyst returns truncated response
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="truncated analyst shouldn't crash acceptance criteria parsing",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_analyst_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["analyst"])
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-analyst-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: explore returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="empty explore should still proceed with planning",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_explore_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["explore"])
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-explore-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: all fixed-name roles return malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="total malformed cascade on planning/review roles shouldn't crash",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_all_fixed_roles_malformed(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=[
            "explore", "planner", "plan-validator", "analyst",
            "critic", "falsifiability-judge", "verify-judge",
        ],
    )
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-all-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="heterogeneous failures across team roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["analyst"],
        empty_roles=["critic"],
        garbage_input_roles=["plan-validator"],
    )
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage plan-validator response
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="garbage plan-validator output shouldn't crash plan approval",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_plan_validator(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["plan-validator"])
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-garbage-validator")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: critic returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="team",
    traces_bug="empty critic finding shouldn't block PRD generation",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_critic_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["critic"])
    fake = make_scenario_fake(_team_base_fake, config)
    result = await _run_team(tmp_path, fake, "team-sc-critic-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
