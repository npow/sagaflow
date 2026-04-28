"""Scenario reliability tests for ProposalReviewWorkflow.

8 adversarial scenarios testing graceful degradation under failure injection.
Each scenario uses make_scenario_fake to wrap a happy-path base fake with
deterministic failure injections declared via ScenarioConfig.
"""

from __future__ import annotations

import json

from temporalio import activity

from sagaflow.durable.activities import SpawnSubagentInput
from skills.proposal_reviewer.workflow import ProposalReviewInput, ProposalReviewWorkflow

from tests.scenarios.helpers import (
    assert_inbox_reflects_outcome,
    assert_no_hidden_failure,
    run_scenario_workflow,
)
from tests.scenarios.primitives import ScenarioConfig, make_scenario_fake
from tests.scenarios.registry import scenario


# ---------------------------------------------------------------------------
# Base happy-path fake for proposal-reviewer
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _pr_base_fake(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "claim-extractor":
        return {
            "CLAIMS": json.dumps([
                {"id": "c1", "text": "Revenue will triple in 18 months.", "tier": "core"},
                {"id": "c2", "text": "Technology is patentable.", "tier": "supporting"},
            ])
        }
    if role == "critic":
        return {
            "WEAKNESSES": json.dumps([
                {
                    "id": "w1",
                    "title": "Revenue projection lacks evidence",
                    "severity": "high",
                    "dimension": "evidence",
                    "scenario": "No comparable market data.",
                    "counter_response": "Author cites internal projections only.",
                }
            ])
        }
    if role == "fact-check":
        return {
            "VERDICT": "PARTIALLY_TRUE",
            "CONFIDENCE": "medium",
            "EVIDENCE": "Market data supports 50% growth, not 200%.",
        }
    if role == "credibility-judge-1":
        return {"VERDICT_PASS_1": "CREDIBLE"}
    if role == "credibility-judge-2":
        return {"VERDICT_FINAL": "CREDIBLE", "CONFIDENCE": "high"}
    if role == "severity-judge-1":
        return {"FALSIFIABLE": "yes", "SEVERITY_PASS_1": "major"}
    if role == "severity-judge-2":
        return {"FALSIFIABLE": "yes", "SEVERITY_FINAL": "major", "FIXABILITY": "fixable"}
    if role == "landscape-judge":
        return {
            "MARKET_WINDOW": "closing",
            "PLATFORM_RISK": "medium",
            "BLIND_SPOT": "Competitor X launching similar product Q3.",
        }
    if role == "rationalization-auditor":
        return {
            "REPORT_FIDELITY": "clean",
            "SUSPICIOUS_PATTERN": "none",
            "COMPROMISED_COUNT": "0",
        }
    if role == "synth":
        return {"REPORT": "# Proposal Review\n\n2 claims, 1 weakness. Mixed evidence.\n"}
    return {}


def _make_input(tmp_path, run_id: str) -> ProposalReviewInput:
    return ProposalReviewInput(
        run_id=run_id,
        proposal_text="Revenue will triple in 18 months. Technology is patentable and defensible.",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        notify=False,
    )


async def _run_pr(tmp_path, fake_spawn, run_id: str) -> str:
    inp = _make_input(tmp_path, run_id)
    return await run_scenario_workflow(
        tmp_path,
        ProposalReviewWorkflow,
        inp,
        fake_spawn,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Scenario 1: all critics return malformed — quorum failure
# ---------------------------------------------------------------------------


@scenario(
    skill="proposal-reviewer",
    traces_bug="malformed critics should trigger quorum failure, not crash",
    failure_modes=["malformed"],
    tags=["degradation"],
)
async def test_all_critics_malformed(tmp_path) -> None:
    config = ScenarioConfig(malformed_roles=["critic"])
    fake = make_scenario_fake(_pr_base_fake, config)
    result = await _run_pr(tmp_path, fake, "pr-sc-critics-malformed")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 2: synth produces truncated output
# ---------------------------------------------------------------------------


@scenario(
    skill="proposal-reviewer",
    traces_bug="truncated synth should still produce a review file",
    failure_modes=["truncated"],
    tags=["degradation"],
)
async def test_synth_truncated(tmp_path) -> None:
    config = ScenarioConfig(truncated_roles=["synth"])
    fake = make_scenario_fake(_pr_base_fake, config)
    result = await _run_pr(tmp_path, fake, "pr-sc-synth-truncated")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 3: claim extractor returns empty
# ---------------------------------------------------------------------------


@scenario(
    skill="proposal-reviewer",
    traces_bug="empty claim extraction should still run critique pipeline",
    failure_modes=["empty"],
    tags=["degradation"],
)
async def test_claim_extractor_empty(tmp_path) -> None:
    config = ScenarioConfig(empty_roles=["claim-extractor"])
    fake = make_scenario_fake(_pr_base_fake, config)
    result = await _run_pr(tmp_path, fake, "pr-sc-claims-empty")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 4: credibility judges disagree
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_pr_judges_disagree(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "claim-extractor":
        return {
            "CLAIMS": json.dumps([
                {"id": "c1", "text": "Revenue will triple.", "tier": "core"},
            ])
        }
    if role == "critic":
        return {
            "WEAKNESSES": json.dumps([
                {
                    "id": "w1",
                    "title": "No evidence",
                    "severity": "high",
                    "dimension": "evidence",
                    "scenario": "No data.",
                    "counter_response": "Author says trust me.",
                }
            ])
        }
    if role == "fact-check":
        return {"VERDICT": "UNVERIFIABLE", "CONFIDENCE": "low"}
    if role == "credibility-judge-1":
        return {"VERDICT_PASS_1": "CREDIBLE"}
    if role == "credibility-judge-2":
        return {"VERDICT_FINAL": "NOT_CREDIBLE", "CONFIDENCE": "high"}
    if role == "severity-judge-1":
        return {"FALSIFIABLE": "yes", "SEVERITY_PASS_1": "critical"}
    if role == "severity-judge-2":
        return {"FALSIFIABLE": "yes", "SEVERITY_FINAL": "minor", "FIXABILITY": "fixable"}
    if role == "landscape-judge":
        return {"MARKET_WINDOW": "open", "PLATFORM_RISK": "low", "BLIND_SPOT": "None."}
    if role == "rationalization-auditor":
        return {"REPORT_FIDELITY": "clean", "SUSPICIOUS_PATTERN": "none", "COMPROMISED_COUNT": "0"}
    if role == "synth":
        return {"REPORT": "# Review\n\nJudges disagreed on credibility.\n"}
    return {}


@scenario(
    skill="proposal-reviewer",
    traces_bug="contradictory judge verdicts should use pass-2 as authoritative",
    failure_modes=["conflicting_verdicts"],
    tags=["correctness"],
)
async def test_judges_disagree(tmp_path) -> None:
    result = await _run_pr(tmp_path, _fake_pr_judges_disagree, "pr-sc-judges-disagree")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
    assert_inbox_reflects_outcome(tmp_path, "DONE", "pr-sc-judges-disagree")


# ---------------------------------------------------------------------------
# Scenario 5: fact-check gets model error
# ---------------------------------------------------------------------------


@scenario(
    skill="proposal-reviewer",
    traces_bug="model error on fact-check shouldn't lose prior claims/weaknesses",
    failure_modes=["model_error"],
    tags=["resilience"],
)
async def test_fact_check_model_error(tmp_path) -> None:
    config = ScenarioConfig(model_error_roles=["fact-check"])
    fake = make_scenario_fake(_pr_base_fake, config)
    result = await _run_pr(tmp_path, fake, "pr-sc-factcheck-error")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 6: mixed failures across roles
# ---------------------------------------------------------------------------


@scenario(
    skill="proposal-reviewer",
    traces_bug="heterogeneous failures across review roles — realistic scenario",
    failure_modes=["malformed", "empty", "garbage_input"],
    tags=["mixed", "resilience"],
)
async def test_mixed_failures(tmp_path) -> None:
    config = ScenarioConfig(
        malformed_roles=["credibility-judge-1"],
        empty_roles=["landscape-judge"],
        garbage_input_roles=["rationalization-auditor"],
    )
    fake = make_scenario_fake(_pr_base_fake, config)
    result = await _run_pr(tmp_path, fake, "pr-sc-mixed-failures")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 7: garbage critic responses
# ---------------------------------------------------------------------------


@scenario(
    skill="proposal-reviewer",
    traces_bug="garbage critic output shouldn't crash weakness parsing",
    failure_modes=["garbage_input"],
    tags=["degradation"],
)
async def test_garbage_critics(tmp_path) -> None:
    config = ScenarioConfig(garbage_input_roles=["critic"])
    fake = make_scenario_fake(_pr_base_fake, config)
    result = await _run_pr(tmp_path, fake, "pr-sc-garbage-critics")
    assert result is not None
    assert_no_hidden_failure(tmp_path)


# ---------------------------------------------------------------------------
# Scenario 8: auditor and landscape judge both fail
# ---------------------------------------------------------------------------


@scenario(
    skill="proposal-reviewer",
    traces_bug="late-stage judge failures shouldn't block report generation",
    failure_modes=["model_error", "malformed"],
    tags=["resilience"],
)
async def test_auditor_and_landscape_fail(tmp_path) -> None:
    config = ScenarioConfig(
        model_error_roles=["rationalization-auditor"],
        malformed_roles=["landscape-judge"],
    )
    fake = make_scenario_fake(_pr_base_fake, config)
    result = await _run_pr(tmp_path, fake, "pr-sc-auditor-landscape-fail")
    assert result is not None
    assert_no_hidden_failure(tmp_path)
