"""Scenario reliability tests for LoopUntilDoneWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.loop_until_done.workflow import LoopUntilDoneInput, LoopUntilDoneWorkflow

from tests.scenarios.helpers import (
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for loop-until-done
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _lud_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "prd":
        return {
            "STORIES": json.dumps([
                {
                    "id": "s1",
                    "title": "Implement hello endpoint",
                    "criteria": [
                        {
                            "id": "c1",
                            "criterion": "GET /hello returns 200",
                            "verification_command": "curl -s http://localhost:8080/hello",
                            "expected_pattern": "200",
                        }
                    ],
                }
            ])
        }
    if role == "falsifiability":
        return {
            "CRITERION_VERDICTS": json.dumps([
                {"criterion_id": "c1", "pass": True, "rationale": "Observable HTTP status code."}
            ])
        }
    if role == "executor":
        return {"WORK_DESCRIPTION": "Created GET /hello endpoint returning HTTP 200."}
    if role == "verifier":
        return {"VERIFIED": "true"}
    if role == "reviewer":
        return {"OVERALL_VERDICT": "all_stories_passed"}
    return {}


def _make_input(tmp_path, run_id: str, max_iter: int = 1) -> LoopUntilDoneInput:
    return LoopUntilDoneInput(
        run_id=run_id,
        task="Build a hello endpoint",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_iter=max_iter,
        notify=False,
    )


async def _run_lud(tmp_path, fake_spawn, run_id: str, max_iter: int = 1) -> str:
    inp = _make_input(tmp_path, run_id, max_iter)
    return await run_scenario_workflow(
        tmp_path,
        LoopUntilDoneWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: executor returns malformed output
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="malformed executor response shouldn't block verification",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_executor_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["executor"])
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-exec-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: verifier gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="model error on verifier shouldn't lose executor work",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_verifier_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["verifier"])
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-verifier-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: reviewer returns truncated response
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="truncated reviewer shouldn't crash loop termination",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_reviewer_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["reviewer"])
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-reviewer-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: prd returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="empty PRD should terminate gracefully with clear label",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_prd_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["prd"])
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-prd-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: all roles return malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="total malformed cascade shouldn't crash — should degrade gracefully",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_all_roles_malformed(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["prd", "falsifiability", "executor", "verifier", "reviewer"],
    )
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-all-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="heterogeneous failures across loop roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["verifier"],
        empty_roles=["falsifiability"],
        garbage_input_roles=["reviewer"],
    )
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage executor response
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="garbage executor output shouldn't crash work verification",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_executor(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["executor"])
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-garbage-executor")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: falsifiability returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="loop-until-done",
    traces_bug="empty falsifiability check shouldn't block execution loop",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_falsifiability_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["falsifiability"])
    fake = make_scenario_fake(_lud_base_fake, config)
    result = await _run_lud(tmp_path, fake, "lud-sc-falsifiability-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
