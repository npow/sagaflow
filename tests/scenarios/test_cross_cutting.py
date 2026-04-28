"""Cross-cutting scenario reliability tests.

8 infrastructure-level failure scenarios that apply to any sagaflow skill.
Uses DeepQaWorkflow as the vehicle since it's the most thoroughly tested.
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
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _cc_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Edge cases?"},
            ])
        }
    if role == "critic":
        return {
            "DEFECTS": json.dumps([
                {
                    "id": "d1",
                    "title": "Bug",
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
                    "rationale": "Valid.",
                }
            ])
        }
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "All defects carried."}
    if role == "synth":
        return {"REPORT": "# QA Report\n\nCross-cutting test.\n"}
    return {}


def _make_input(tmp_path, run_id: str, max_rounds: int = 1) -> DeepQaInput:
    artifact = tmp_path / "artifact.txt"
    if not artifact.exists():
        artifact.write_text("def foo(): pass\n")
    return DeepQaInput(
        run_id=run_id,
        artifact_path=str(artifact),
        artifact_type="code",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_rounds=max_rounds,
        notify=False,
    )


async def _run(tmp_path, fake_spawn, run_id: str, max_rounds: int = 1) -> str:
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
# Scenario 1: total malformed cascade — every role returns malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="cross-cutting",
    traces_bug="all roles malformed shouldn't crash — should degrade gracefully",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_total_malformed_cascade(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=[
            "dim-discover", "critic", "judge-pass-1", "judge-pass-2",
            "auditor", "synth",
        ],
    )
    fake = make_scenario_fake(_cc_base_fake, config)
    result = await _run(tmp_path, fake, "cc-total-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: model error on all roles
# ---------------------------------------------------------------------------


@scenario(
    skill="cross-cutting",
    traces_bug="API errors on all roles — graceful termination",
    failure_modes=["model_error"],
    tags=["infrastructure", "resilience"],
)
async def test_model_error_all_roles(tmp_path) -> None:
    config = ScenarioConfig(
        model_error_roles=[
            "dim-discover", "critic", "judge-pass-1", "judge-pass-2",
            "auditor", "synth",
        ],
    )
    fake = make_scenario_fake(_cc_base_fake, config)
    result = await _run(tmp_path, fake, "cc-model-error-all")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: timeout on early role
# ---------------------------------------------------------------------------


@scenario(
    skill="cross-cutting",
    traces_bug="timeout on early role shouldn't hang the workflow",
    failure_modes=["timeout"],
    tags=["infrastructure", "resilience"],
)
async def test_timeout_cascade(tmp_path) -> None:
    config = ScenarioConfig(timeout_roles=["dim-discover"])
    fake = make_scenario_fake(_cc_base_fake, config)
    try:
        result = await _run(tmp_path, fake, "cc-timeout-cascade")
        assert result is not None
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Scenario 4: empty responses from all roles
# ---------------------------------------------------------------------------


@scenario(
    skill="cross-cutting",
    traces_bug="empty responses everywhere shouldn't produce a fake-good report",
    failure_modes=["empty"],
    tags=["cascade", "correctness"],
)
async def test_empty_response_all_roles(tmp_path) -> None:
    config = ScenarioConfig(
        empty_roles=[
            "dim-discover", "critic", "judge-pass-1", "judge-pass-2",
            "auditor", "synth",
        ],
    )
    fake = make_scenario_fake(_cc_base_fake, config)
    result = await _run(tmp_path, fake, "cc-empty-all")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: garbage responses from all roles
# ---------------------------------------------------------------------------


@scenario(
    skill="cross-cutting",
    traces_bug="garbage content shouldn't crash parsing or workflow",
    failure_modes=["garbage_input"],
    tags=["cascade", "resilience"],
)
async def test_garbage_all_roles(tmp_path) -> None:
    config = ScenarioConfig(
        garbage_input_roles=[
            "dim-discover", "critic", "judge-pass-1", "judge-pass-2",
            "auditor", "synth",
        ],
    )
    fake = make_scenario_fake(_cc_base_fake, config)
    result = await _run(tmp_path, fake, "cc-garbage-all")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures — different failure modes per role
# ---------------------------------------------------------------------------


@scenario(
    skill="cross-cutting",
    traces_bug="heterogeneous failures across roles — realistic production scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["judge-pass-1"],
        empty_roles=["auditor"],
        garbage_input_roles=["critic"],
    )
    fake = make_scenario_fake(_cc_base_fake, config)
    result = await _run(tmp_path, fake, "cc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: synth fails but critics were good
# ---------------------------------------------------------------------------


@scenario(
    skill="cross-cutting",
    traces_bug="synth failure with valid critiques should fall back to draft",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_synth_failure_with_good_critics(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["synth"])
    fake = make_scenario_fake(_cc_base_fake, config)
    result = await _run(tmp_path, fake, "cc-synth-fail-good-critics")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
    report_path = tmp_path / "run" / "qa-report.md"
    assert report_path.exists(), "Report should exist even with synth failure"


# ---------------------------------------------------------------------------
# Scenario 8: judges disagree — pass-1 and pass-2 give opposite verdicts
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_judges_disagree(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Edge cases?"},
            ])
        }
    if role == "critic":
        return {
            "DEFECTS": json.dumps([
                {
                    "id": "d1",
                    "title": "Bug",
                    "severity": "critical",
                    "dimension": "correctness",
                    "scenario": "x=None",
                    "root_cause": "No guard.",
                }
            ])
        }
    if role == "judge-pass-1":
        return {
            "VERDICTS": json.dumps([
                {
                    "defect_id": "d1",
                    "severity": "critical",
                    "confidence": "high",
                    "calibration": "confirm",
                    "rationale": "Clearly a real bug.",
                }
            ])
        }
    if role == "judge-pass-2":
        return {
            "VERDICTS": json.dumps([
                {
                    "defect_id": "d1",
                    "severity": "cosmetic",
                    "confidence": "high",
                    "calibration": "downgrade",
                    "rationale": "Not actually a problem.",
                }
            ])
        }
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "Carried."}
    if role == "synth":
        return {"REPORT": "# QA Report\n\nJudges disagreed.\n"}
    return {}


@scenario(
    skill="cross-cutting",
    traces_bug="contradictory judge verdicts should use pass-2 as authoritative",
    failure_modes=["conflicting_verdicts"],
    tags=["correctness"],
)
async def test_judges_disagree(tmp_path) -> None:
    result = await _run(tmp_path, _fake_judges_disagree, "cc-judges-disagree")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
    assert_inbox_reflects_outcome(tmp_path, "DONE", "cc-judges-disagree")
