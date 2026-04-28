"""Scenario reliability tests for DeepDesignWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.deep_design.workflow import DeepDesignInput, DeepDesignWorkflow

from tests.scenarios.helpers import (
    assert_inbox_reflects_outcome,
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for deep-design
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _dd_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "draft":
        return {
            "SPEC": (
                "# Widget Service\n\n"
                "## Overview\nA widget management service.\n\n"
                "## Goals\n- CRUD operations\n- Event-driven updates\n\n"
                "## Components\n- API Gateway, Worker Pool, Event Bus\n"
            )
        }
    if role == "fact-sheet":
        return {
            "RECOVERY_BEHAVIORS": json.dumps([
                {"component": "API Gateway", "failure": "timeout", "recovery": "retry with backoff"},
            ])
        }
    if role == "critic":
        return {
            "FLAWS": json.dumps([
                {
                    "id": "f1",
                    "title": "No rate limiting",
                    "severity": "major",
                    "dimension": "scalability",
                    "scenario": "Burst traffic overwhelms API.",
                },
            ]),
            "GAP_REPORTS": "[]",
        }
    if role == "outside-frame":
        return {"FLAWS": "[]", "GAP_REPORTS": "[]"}
    if role in ("judge-pass-1", "judge-pass-2"):
        return {
            "VERDICT": json.dumps([
                {
                    "flaw_id": "f1",
                    "severity": "major",
                    "calibration": "confirm",
                    "rationale": "Valid flaw.",
                }
            ])
        }
    if role == "challenger":
        return {"CHALLENGE": "No challenge needed."}
    if role == "cross-fix":
        return {"CONFLICTS": "[]"}
    if role == "redesign":
        return {
            "SPEC": (
                "# Widget Service (v2)\n\n"
                "## Overview\nA widget management service with rate limiting.\n\n"
                "## Goals\n- CRUD operations\n- Event-driven updates\n- Rate limiting\n\n"
                "## Components\n- API Gateway, Worker Pool, Event Bus, Rate Limiter\n"
            ),
            "COMPONENTS_ADDED": "1",
        }
    if role == "invariant-validator":
        return {"VIOLATIONS": "[]"}
    if role == "drift-judge":
        return {"DRIFT_SCORE": "0.1", "DRIFT_VERDICT": "acceptable"}
    if role == "synth":
        return {"REPORT": "# Widget Service — Final Spec\n\n1 major flaw addressed.\n"}
    return {}


def _make_input(tmp_path, run_id: str, max_rounds: int = 1) -> DeepDesignInput:
    return DeepDesignInput(
        run_id=run_id,
        concept="A widget management service with CRUD and event-driven updates.",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_rounds=max_rounds,
        notify=False,
    )


async def _run_dd(tmp_path, fake_spawn, run_id: str, max_rounds: int = 1) -> str:
    inp = _make_input(tmp_path, run_id, max_rounds)
    return await run_scenario_workflow(
        tmp_path,
        DeepDesignWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: all critics return malformed responses
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-design",
    traces_bug="malformed critics shouldn't crash the design workflow",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_all_critics_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["critic", "outside-frame"])
    fake = make_scenario_fake(_dd_base_fake, config)
    result = await _run_dd(tmp_path, fake, "dd-sc-critics-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: synth produces truncated output
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-design",
    traces_bug="truncated synth output should fall back to last spec revision",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_synth_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["synth"])
    fake = make_scenario_fake(_dd_base_fake, config)
    result = await _run_dd(tmp_path, fake, "dd-sc-synth-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: redesign agent returns model error
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-design",
    traces_bug="model error on redesign shouldn't lose accepted flaws",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_redesign_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["redesign"])
    fake = make_scenario_fake(_dd_base_fake, config)
    result = await _run_dd(tmp_path, fake, "dd-sc-redesign-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: draft returns empty spec
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-design",
    traces_bug="empty draft should still attempt critique rounds",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_draft_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["draft"])
    fake = make_scenario_fake(_dd_base_fake, config)
    result = await _run_dd(tmp_path, fake, "dd-sc-draft-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: judges disagree on severity
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_dd_judges_disagree(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "draft":
        return {
            "SPEC": (
                "# Widget Service\n\n"
                "## Overview\nA widget management service.\n\n"
                "## Goals\n- CRUD operations\n- Event-driven updates\n\n"
                "## Components\n- API Gateway, Worker Pool, Event Bus\n"
            )
        }
    if role == "critic":
        return {
            "FLAWS": json.dumps([
                {
                    "id": "f1",
                    "title": "No auth",
                    "severity": "critical",
                    "dimension": "security",
                    "scenario": "Unauthenticated access.",
                },
            ]),
            "GAP_REPORTS": "[]",
        }
    if role == "judge-pass-1":
        return {
            "VERDICT": json.dumps([
                {"flaw_id": "f1", "severity": "critical", "calibration": "confirm", "rationale": "Real issue."},
            ])
        }
    if role == "judge-pass-2":
        return {
            "VERDICT": json.dumps([
                {"flaw_id": "f1", "severity": "cosmetic", "calibration": "downgrade", "rationale": "Overblown."},
            ])
        }
    if role == "redesign":
        return {
            "SPEC": "# Widget Service (v2)\n\nWith auth.\n",
            "COMPONENTS_ADDED": "1",
        }
    if role == "synth":
        return {"REPORT": "# Widget Service — Final\n\nJudges disagreed.\n"}
    return {}


@scenario(
    skill="deep-design",
    traces_bug="contradictory judge verdicts should use pass-2 as authoritative",
    failure_modes=["conflicting_verdicts"],
    tags=["correctness"],
)
async def test_judges_disagree(tmp_path) -> None:
    result = await _run_dd(tmp_path, _fake_dd_judges_disagree, "dd-sc-judges-disagree")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
    assert_inbox_reflects_outcome(tmp_path, "DONE", "dd-sc-judges-disagree")


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-design",
    traces_bug="heterogeneous failures across design roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["judge-pass-1"],
        empty_roles=["fact-sheet"],
        garbage_input_roles=["outside-frame"],
    )
    fake = make_scenario_fake(_dd_base_fake, config)
    result = await _run_dd(tmp_path, fake, "dd-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage critic responses
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-design",
    traces_bug="garbage critic output shouldn't crash flaw parsing",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_critics(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["critic"])
    fake = make_scenario_fake(_dd_base_fake, config)
    result = await _run_dd(tmp_path, fake, "dd-sc-garbage-critics")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: invariant validator and drift judge both fail
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-design",
    traces_bug="validation gate failures shouldn't block spec output",
    failure_modes=["model_error", "malformed"],
    tags=["resilience"],
)
async def test_validation_gates_fail(tmp_path) -> None:
    config = ScenarioConfig(
        model_error_roles=["invariant-validator"],
        malformed_roles=["drift-judge"],
    )
    fake = make_scenario_fake(_dd_base_fake, config)
    result = await _run_dd(tmp_path, fake, "dd-sc-validation-fail")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
