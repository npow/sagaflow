"""Scenario reliability tests for DeepResearchWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from sagaflow.slack_progress import report_slack_progress
from skills.deep_research.workflow import DeepResearchInput, DeepResearchWorkflow

from tests.scenarios.helpers import (
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for deep-research
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _dr_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "lang-detect":
        return {
            "AUTHORITATIVE_LANGUAGES": '["en"]',
            "COVERAGE_EXPECTATION": "en_dominant",
        }
    if role == "novelty-classify":
        return {
            "NOVELTY_CLASS": "familiar",
            "RECALLED_SOURCES": json.dumps([
                {"title": "Paper A", "authors_or_org": "Org1", "year": 2022, "confidence": "high"},
                {"title": "Paper B", "authors_or_org": "Org2", "year": 2023, "confidence": "high"},
                {"title": "Paper C", "authors_or_org": "Org3", "year": 2021, "confidence": "medium"},
            ]),
            "VERIFIED_COUNT": "3",
        }
    if role == "vocab-bootstrap":
        return {
            "CANONICAL_TERMS": '["term-alpha", "term-beta", "term-gamma"]',
            "DISCOVERED_SOURCES": '["https://en.wikipedia.org/wiki/Example"]',
        }
    if role == "dim-discover":
        dirs = [
            {"id": "d1", "dimension": "HOW", "question": "How does it work?", "priority": "high"},
            {"id": "d2", "dimension": "WHO", "question": "Who uses it?", "priority": "medium"},
            {"id": "d3", "dimension": "PRIOR-FAILURE", "question": "What failed before?", "priority": "high"},
            {"id": "d4", "dimension": "BASELINE", "question": "What is the baseline?", "priority": "high"},
            {"id": "d5", "dimension": "ADJACENT-EFFORTS", "question": "Adjacent work?", "priority": "medium"},
        ]
        return {"DIRECTIONS": json.dumps(dirs)}
    if role == "researcher":
        return {
            "FINDINGS": "Summary of research findings.",
            "SOURCES": '["Source A", "Source B"]',
            "CLAIMS": json.dumps([
                {"claim": "X costs 42 units", "source": "Source A",
                 "corroboration": "single_source", "recency_class": "fresh"},
            ]),
        }
    if role == "coord-summary":
        return {
            "COORD_SUMMARY": "## Round Summary\n\nMainstream: covered.\nCounter-narratives: none.",
        }
    if role == "verifier":
        return {
            "VERIFIED": '["claim-0"]',
            "MISMATCHES": '[{"claim_id": "claim-1", "issue": "number mismatch"}]',
            "UNVERIFIABLE": "[]",
            "SAMPLING_STRATEGY": '{"single_source": 1, "numerical": 1, "contested": 0, "other": 0}',
        }
    if role == "synth":
        return {
            "REPORT": (
                "# Research Report\n\n"
                "## Executive Summary\n\nFindings synthesized.\n\n"
                "## Fact Verification Results\n\nVerified: 1, Mismatches: 1\n"
            ),
        }
    return {}


def _make_input(tmp_path, run_id: str) -> DeepResearchInput:
    return DeepResearchInput(
        run_id=run_id,
        seed="Impact of caching strategies on API latency",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_rounds=1,
        max_directions=5,
        notify=False,
    )


async def _run_dr(tmp_path, fake_spawn, run_id: str) -> str:
    inp = _make_input(tmp_path, run_id)
    return await run_scenario_workflow(
        tmp_path,
        DeepResearchWorkflow,
        inp,
        fake_spawn,
        extra_activities=[report_slack_progress],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: researcher returns malformed output
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="malformed researcher response shouldn't crash coordination",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_researcher_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["researcher"])
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-researcher-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: verifier gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="model error on verifier shouldn't lose research findings",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_verifier_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["verifier"])
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-verifier-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: synth returns truncated output
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="truncated synth should fall back to raw findings",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_synth_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["synth"])
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-synth-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: dim-discover returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="empty dim-discover should still attempt synthesis",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_dim_discover_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["dim-discover"])
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-dim-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 5: all roles return malformed
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="total malformed cascade shouldn't crash — should degrade gracefully",
    failure_modes=["malformed"],
    tags=["cascade", "resilience"],
)
async def test_all_roles_malformed(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=[
            "lang-detect", "novelty-classify", "vocab-bootstrap",
            "dim-discover", "researcher", "coord-summary", "verifier", "synth",
        ],
    )
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-all-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="heterogeneous failures across research roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["verifier"],
        empty_roles=["novelty-classify"],
        garbage_input_roles=["coord-summary"],
    )
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage researcher response
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="garbage researcher output shouldn't crash claim extraction",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_researcher(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["researcher"])
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-garbage-researcher")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: novelty-classify returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="deep-research",
    traces_bug="empty novelty classification shouldn't block research rounds",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_novelty_classify_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["novelty-classify"])
    fake = make_scenario_fake(_dr_base_fake, config)
    result = await _run_dr(tmp_path, fake, "dr-sc-novelty-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
