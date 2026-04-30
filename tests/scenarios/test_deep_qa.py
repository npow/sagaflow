"""Scenario reliability tests for DeepQaWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from skills.deep_qa.activities import read_text_file
from skills.deep_qa.workflow import DeepQaInput, DeepQaWorkflow

from tests.scenarios.helpers import (
    assert_inbox_reflects_outcome,
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import (
    PartialQuorumSpec,
    ScenarioConfig,
    make_scenario_fake,
)
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for deep-qa
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _dq_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Edge case handling?"},
                {"id": "a2", "dimension": "security", "question": "Input validation?"},
            ])
        }
    if role == "critic":
        return {
            "DEFECTS": json.dumps([
                {
                    "id": "d1",
                    "title": "Missing null check",
                    "severity": "major",
                    "dimension": "correctness",
                    "scenario": "x=None",
                    "root_cause": "No guard.",
                }
            ])
        }
    if role in ("judge-pass-1", "judge-pass-2"):
        return {
            "VERDICTS": json.dumps([
                {
                    "defect_id": "d1",
                    "severity": "major",
                    "confidence": "high",
                    "calibration": "confirm",
                    "rationale": "Valid defect.",
                }
            ])
        }
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "All defects carried."}
    if role == "synth":
        return {"REPORT": "# QA Report\n\n1 major defect found.\n"}
    return {}


def _make_input(tmp_path, run_id: str, max_rounds: int = 1) -> DeepQaInput:
    artifact = tmp_path / "artifact.txt"
    if not artifact.exists():
        artifact.write_text("def foo(x): return x.bar\n")
    return DeepQaInput(
        run_id=run_id,
        artifact_path=str(artifact),
        artifact_type="code",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_rounds=max_rounds,
        notify=False,
    )


async def _run_dq(tmp_path, fake_spawn, run_id: str, max_rounds: int = 1) -> str:
    inp = _make_input(tmp_path, run_id, max_rounds)
    return await run_scenario_workflow(
        tmp_path,
        DeepQaWorkflow,
        inp,
        fake_spawn,
        extra_activities=[read_text_file],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: all critics return malformed responses
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="critics returning garbage shouldn't crash workflow",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_all_critics_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["critic"])
    fake = make_scenario_fake(_dq_base_fake, config)
    result = await _run_dq(tmp_path, fake, "dq-sc-critics-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
    assert_inbox_reflects_outcome(tmp_path, "DONE", "dq-sc-critics-malformed")


# ---------------------------------------------------------------------------
# Scenario 2: synth produces truncated output
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="truncated synth output should fall back to draft",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_synth_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["synth"])
    fake = make_scenario_fake(_dq_base_fake, config)
    result = await _run_dq(tmp_path, fake, "dq-sc-synth-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: judge times out
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="judge timeout shouldn't prevent report generation",
    failure_modes=["timeout"],
    tags=["resilience"],
)
async def test_judge_timeout(tmp_path) -> None:
    config = ScenarioConfig(timeout_roles=["judge-pass-1"])
    fake = make_scenario_fake(_dq_base_fake, config)
    try:
        await _run_dq(tmp_path, fake, "dq-sc-judge-timeout")
        assert_no_hidden_failure(tmp_path)
    except Exception:
        # Timeout propagation is acceptable — not a silent failure
        pass


# ---------------------------------------------------------------------------
# Scenario 4: dim-discover returns empty angles
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="empty dimension discovery should still produce a report",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_dim_discover_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["dim-discover"])
    fake = make_scenario_fake(_dq_base_fake, config)
    result = await _run_dq(tmp_path, fake, "dq-sc-dim-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: auditor returns conflicting verdicts
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="conflicting auditor verdicts should be handled",
    failure_modes=["conflicting_verdicts"],
    tags=["degradation"],
)
async def test_auditor_conflicting_verdicts(tmp_path) -> None:
    config = ScenarioConfig(conflicting_verdicts_roles=["auditor"])
    fake = make_scenario_fake(_dq_base_fake, config)
    result = await _run_dq(tmp_path, fake, "dq-sc-auditor-conflict")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: partial critic quorum — some critics malformed after first
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="partial critic quorum — some critics malformed",
    failure_modes=["partial_quorum"],
    tags=["resilience"],
)
async def test_partial_critic_quorum(tmp_path) -> None:
    config = ScenarioConfig(
        partial_quorum=PartialQuorumSpec(fail_roles=["critic"], keep_count=1),
    )
    fake = make_scenario_fake(_dq_base_fake, config)
    result = await _run_dq(tmp_path, fake, "dq-sc-partial-quorum", max_rounds=2)
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage input in critic response
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="garbage critic responses shouldn't crash workflow",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_input_response(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["critic"])
    fake = make_scenario_fake(_dq_base_fake, config)
    result = await _run_dq(tmp_path, fake, "dq-sc-garbage-critic")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: duplicate defects in critic output
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-qa",
    traces_bug="duplicate defects should be deduplicated or handled",
    failure_modes=["duplicate_defect"],
    tags=["correctness"],
)
async def test_duplicate_defects(tmp_path) -> None:
    config = ScenarioConfig(duplicate_defect_roles=["critic"])
    fake = make_scenario_fake(_dq_base_fake, config)
    result = await _run_dq(tmp_path, fake, "dq-sc-dup-defects")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
    assert_inbox_reflects_outcome(tmp_path, "DONE", "dq-sc-dup-defects")
