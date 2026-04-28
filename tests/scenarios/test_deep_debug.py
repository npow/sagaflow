"""Scenario reliability tests for DeepDebugWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.deep_debug.workflow import DeepDebugInput, DeepDebugWorkflow

from tests.scenarios.helpers import (
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for deep-debug
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _ddb_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "premortem":
        return {"BLIND_SPOTS": json.dumps(["Watch out for concurrency assumptions"])}
    if role == "hypothesis":
        return {
            "HYP_ID": "h0",
            "DIMENSION": "concurrency",
            "MECHANISM": "Shared state read during a write.",
            "EVIDENCE_TIER": "3",
            "PLAUSIBILITY": "leading",
            "CONFIDENCE": "medium",
        }
    if role == "outside-frame":
        return {
            "HYP_ID": "outside-frame",
            "DIMENSION": "infrastructure",
            "MECHANISM": "DNS resolution intermittently fails.",
            "CONFIDENCE": "low",
        }
    if role == "judge-pass-1":
        return {
            "VERDICTS": json.dumps([
                {"hyp_id": "c1-h0", "plausibility": "leading",
                 "evidence_tier": "3", "falsifiable": "true",
                 "rationale": "Mechanism is specific and falsifiable"},
            ])
        }
    if role == "judge-pass-2":
        return {
            "VERDICTS": json.dumps([
                {"hyp_id": "c1-h0", "plausibility": "leading",
                 "pass2_verdict": "CONFIRM", "rationale": "Confirmed"},
            ])
        }
    if role == "probe":
        return {
            "PROBE_ID": "p1",
            "WINNER": "c1-h0",
            "FALSIFIED": json.dumps(["c1-hOF"]),
            "STATUS": "completed",
        }
    if role == "fix-worker":
        return {"FIX_APPLIED": "true", "TEST_PASSES": "true"}
    if role == "synth":
        return {"REPORT": "# Debug Report\n\nFixed.\n"}
    return {}


def _make_input(tmp_path, run_id: str) -> DeepDebugInput:
    return DeepDebugInput(
        run_id=run_id,
        symptom="Test intermittently fails with AssertionError.",
        reproduction_command="pytest tests/test_thing.py -v",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        num_hypotheses=2,
        max_cycles=1,
        hard_stop=6,
        notify=False,
    )


async def _run_ddb(tmp_path, fake_spawn, run_id: str) -> str:
    inp = _make_input(tmp_path, run_id)
    return await run_scenario_workflow(
        tmp_path,
        DeepDebugWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: hypothesis returns malformed output
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="malformed hypothesis shouldn't crash judge pipeline",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_hypothesis_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["hypothesis"])
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-hyp-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: judge gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="model error on judge shouldn't lose hypothesis rankings",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_judge_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["judge-pass-1"])
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-judge-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: fix-worker returns truncated response
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="truncated fix-worker output shouldn't block synthesis",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_fix_worker_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["fix-worker"])
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-fix-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: synth returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="empty synth should still finalize with whatever evidence exists",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_synth_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["synth"])
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-synth-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: all roles return malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="total malformed cascade shouldn't crash — should degrade gracefully",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_all_roles_malformed(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=[
            "premortem", "hypothesis", "outside-frame",
            "judge-pass-1", "judge-pass-2", "probe", "fix-worker", "synth",
        ],
    )
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-all-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="heterogeneous failures across debug roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["judge-pass-1"],
        empty_roles=["premortem"],
        garbage_input_roles=["outside-frame"],
    )
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: premortem returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="empty premortem shouldn't prevent hypothesis generation",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_premortem_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["premortem"])
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-premortem-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: garbage hypothesis response
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-debug",
    traces_bug="garbage hypothesis output shouldn't crash judge pipeline",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_hypothesis(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["hypothesis"])
    fake = make_scenario_fake(_ddb_base_fake, config)
    result = await _run_ddb(tmp_path, fake, "ddb-sc-garbage-hyp")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
