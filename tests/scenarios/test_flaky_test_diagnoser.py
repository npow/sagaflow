"""Scenario reliability tests for FlakyTestWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.

Note: run_test_subprocess is a separate activity (not spawn_subagent), so
make_scenario_fake does not wrap it — it passes through as an extra activity.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.flaky_test_diagnoser.workflow import FlakyTestInput, FlakyTestWorkflow

from tests.scenarios.helpers import (
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Fake run_test_subprocess — alternates pass/fail
# ---------------------------------------------------------------------------


def _make_fake_run_test_subprocess() -> tuple:
    counter: list[int] = []

    @activity.defn(name="run_test_subprocess")
    async def _fake_run_test(command: str, timeout: int = 60) -> dict[str, int]:
        call_index = len(counter)
        counter.append(call_index)
        exit_code = 1 if call_index % 2 == 1 else 0
        return {"exit_code": exit_code, "duration_ms": 42}

    return _fake_run_test, counter


# ---------------------------------------------------------------------------
# Base happy-path fake for spawn_subagent
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _ftd_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
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
            ])
        }
    if role == "judge":
        return {
            "RANKINGS": json.dumps([
                {"hyp_id": "h2", "rank": 1, "uncertainty": "medium"},
                {"hyp_id": "h1", "rank": 2, "uncertainty": "high"},
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
            ),
            "TERMINATION_LABEL": "narrowed_to_N_hypotheses",
        }
    return {}


def _make_input(tmp_path, run_id: str) -> FlakyTestInput:
    return FlakyTestInput(
        run_id=run_id,
        test_identifier="tests/test_example.py::test_flaky",
        run_dir=str(tmp_path / "run"),
        inbox_path=str(tmp_path / "INBOX.md"),
        run_command="pytest tests/test_example.py::test_flaky -v",
        n_runs=4,
        notify=False,
    )


async def _run_ftd(tmp_path, fake_spawn, run_id: str) -> str:
    fake_run_test, _ = _make_fake_run_test_subprocess()
    inp = _make_input(tmp_path, run_id)
    return await run_scenario_workflow(
        tmp_path,
        FlakyTestWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress, fake_run_test],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: hypothesis-gen returns malformed output
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="malformed hypothesis generation shouldn't crash judge pipeline",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_hypothesis_gen_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["hypothesis-gen"])
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-hyp-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: judge gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="model error on judge shouldn't lose hypothesis rankings",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_judge_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["judge"])
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-judge-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: synth returns truncated output
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="truncated synth should still finalize with available data",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_synth_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["synth"])
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-synth-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: hypothesis-gen returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="empty hypothesis generation should terminate with clear label",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_hypothesis_gen_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["hypothesis-gen"])
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-hyp-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: all spawn_subagent roles return malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="total malformed cascade shouldn't crash — should degrade gracefully",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_all_roles_malformed(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["hypothesis-gen", "judge", "synth"],
    )
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-all-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="heterogeneous failures across diagnoser roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["hypothesis-gen"],
        empty_roles=["judge"],
        garbage_input_roles=["synth"],
    )
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage judge response
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="garbage judge output shouldn't crash ranking extraction",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_judge(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["judge"])
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-garbage-judge")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: synth returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="flaky-test-diagnoser",
    traces_bug="empty synth should still finalize with whatever evidence exists",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_synth_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["synth"])
    fake = make_scenario_fake(_ftd_base_fake, config)
    result = await _run_ftd(tmp_path, fake, "ftd-sc-synth-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
