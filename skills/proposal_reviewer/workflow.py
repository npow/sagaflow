"""proposal-reviewer-temporal: Temporal port of the proposal-reviewer skill.

Phases:
  1. Claim extraction (Sonnet): parse core claims from the proposal.
  2. Parallel critiques on 4 dimensions (Haiku x4): viability, competition,
     structural, evidence.
  3. Fact-check each claim in parallel (Haiku x N, capped at 4).
  4. Assembly (Sonnet): emit REPORT|<markdown> + termination label.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.durable.activities import (
        EmitFindingInput,
        SpawnSubagentInput,
        WriteArtifactInput,
    )
    from sagaflow.durable.retry_policies import HAIKU_POLICY, SONNET_POLICY

# Fixed 4 critique dimensions for Phase 2.
_CRITIQUE_DIMENSIONS = ["viability", "competition", "structural", "evidence"]

# Cap on parallel fact-check calls.
_MAX_FACT_CHECK = 4


@dataclass(frozen=True)
class ProposalReviewInput:
    run_id: str
    proposal_text: str
    inbox_path: str
    run_dir: str
    notify: bool = True


@workflow.defn(name="ProposalReviewWorkflow")
class ProposalReviewWorkflow:
    @workflow.run
    async def run(self, inp: ProposalReviewInput) -> str:
        claim_prompt_path = f"{inp.run_dir}/claim-prompt.txt"
        synth_prompt_path = f"{inp.run_dir}/synth-prompt.txt"
        report_path = f"{inp.run_dir}/review.md"

        # Phase 1: claim extraction.
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=claim_prompt_path,
                content=_claim_extraction_user_prompt(inp.proposal_text),
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        claim_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="claim-extract",
                tier_name="SONNET",
                system_prompt=_claim_extraction_system_prompt(),
                user_prompt_path=claim_prompt_path,
                max_tokens=1024,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=180),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        claims_raw = claim_result.get("CLAIMS", "[]")
        claims = _parse_json_list(claims_raw)

        # Phase 2: 4 parallel critiques.
        critic_prompt_paths: list[str] = []
        for dim in _CRITIQUE_DIMENSIONS:
            ppath = f"{inp.run_dir}/critic-{dim}.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=ppath,
                    content=_critic_user_prompt(inp.proposal_text, dim),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            critic_prompt_paths.append(ppath)

        critic_coros = [
            workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="critic",
                    tier_name="HAIKU",
                    system_prompt=_critic_system_prompt(dim),
                    user_prompt_path=ppath,
                    max_tokens=1024,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=HAIKU_POLICY,
            )
            for dim, ppath in zip(_CRITIQUE_DIMENSIONS, critic_prompt_paths)
        ]
        critic_outputs = await asyncio.gather(*critic_coros, return_exceptions=True)

        all_weaknesses: list[dict[str, str]] = []
        for dim, result in zip(_CRITIQUE_DIMENSIONS, critic_outputs):
            if isinstance(result, BaseException):
                continue
            raw = result.get("WEAKNESSES", "[]")
            for w in _parse_json_list(raw):
                w.setdefault("dimension", dim)
                all_weaknesses.append(w)

        # Phase 3: fact-check each claim in parallel (capped at _MAX_FACT_CHECK).
        claims_to_check = claims[:_MAX_FACT_CHECK]
        fact_check_results: list[dict[str, str]] = []

        if claims_to_check:
            fc_prompt_paths: list[str] = []
            for i, claim in enumerate(claims_to_check):
                ppath = f"{inp.run_dir}/fc-{i}.txt"
                await workflow.execute_activity(
                    "write_artifact",
                    WriteArtifactInput(
                        path=ppath,
                        content=_fact_check_user_prompt(claim),
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=HAIKU_POLICY,
                )
                fc_prompt_paths.append(ppath)

            fc_coros = [
                workflow.execute_activity(
                    "spawn_subagent",
                    SpawnSubagentInput(
                        role="fact-check",
                        tier_name="HAIKU",
                        system_prompt=_fact_check_system_prompt(),
                        user_prompt_path=ppath,
                        max_tokens=512,
                        tools_needed=False,
                    ),
                    start_to_close_timeout=timedelta(seconds=180),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=HAIKU_POLICY,
                )
                for ppath in fc_prompt_paths
            ]
            fc_outputs = await asyncio.gather(*fc_coros, return_exceptions=True)
            for claim, fc_out in zip(claims_to_check, fc_outputs):
                if isinstance(fc_out, BaseException):
                    continue
                fact_check_results.append(
                    {
                        "claim_id": claim.get("id", ""),
                        "verdict": fc_out.get("VERDICT", "UNVERIFIABLE"),
                        "confidence": fc_out.get("CONFIDENCE", "low"),
                    }
                )

        # Phase 4: assembly (Sonnet).
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=synth_prompt_path,
                content=_assembly_user_prompt(
                    proposal_text=inp.proposal_text,
                    claims=claims,
                    weaknesses=all_weaknesses,
                    fact_checks=fact_check_results,
                ),
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        synth_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="synth",
                tier_name="SONNET",
                system_prompt=_assembly_system_prompt(),
                user_prompt_path=synth_prompt_path,
                max_tokens=4096,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=240),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        report_md = synth_result.get(
            "REPORT",
            _fallback_report(claims, all_weaknesses, fact_check_results),
        )

        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=report_path, content=report_md),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        # Determine termination label from verdict distribution.
        termination_label = _termination_label(fact_check_results)

        weak_count = len(all_weaknesses)
        claim_count = len(claims)
        summary = (
            f"{claim_count} claims extracted, {weak_count} weaknesses found; "
            f"label={termination_label}"
        )
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="proposal-reviewer",
                status="DONE",
                summary=summary,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{summary}\nReport: {report_path}"


# --- prompt templates ---


def _claim_extraction_system_prompt() -> str:
    return (
        "You are a proposal analysis agent. Extract the core claims from the proposal "
        "and classify each by tier (core | supporting | peripheral). A 'core' claim is "
        "the central thesis the proposal rests on. Use the STRUCTURED_OUTPUT contract.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        'CLAIMS|[{"id":"c1","text":"<claim>","tier":"core|supporting|peripheral"}, ...]\n'
        "STRUCTURED_OUTPUT_END\n"
        "The CLAIMS value must be valid JSON. Extract 2-6 claims."
    )


def _claim_extraction_user_prompt(proposal_text: str) -> str:
    return (
        f"Proposal length: {len(proposal_text)} chars\n\n"
        "Extract the key claims from this proposal.\n\n"
        "--- PROPOSAL START ---\n"
        f"{proposal_text[:20000]}\n"
        "--- PROPOSAL END ---\n"
    )


def _critic_system_prompt(dimension: str) -> str:
    dim_instructions = {
        "viability": "Focus on technical and operational feasibility. Can this actually be built/executed?",
        "competition": "Focus on the competitive landscape. Are there existing solutions? What differentiates this?",
        "structural": "Focus on logical consistency, gaps in reasoning, and internal contradictions.",
        "evidence": "Focus on the quality and sufficiency of evidence. Are claims backed by data?",
    }
    instruction = dim_instructions.get(dimension, f"Focus on the {dimension} dimension.")
    return (
        f"You are a proposal critic specializing in the {dimension} dimension. "
        f"{instruction} "
        "Find real weaknesses — do not pad. Only flag genuine problems.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        'WEAKNESSES|[{"id":"w1","title":"<short>","severity":"high|medium|low",'
        f'"dimension":"{dimension}","scenario":"<concrete situation>"}}, ...]\n'
        "STRUCTURED_OUTPUT_END\n"
        "If no real weaknesses, emit WEAKNESSES|[]. Do not invent problems."
    )


def _critic_user_prompt(proposal_text: str, dimension: str) -> str:
    return (
        f"Critique dimension: {dimension}\n\n"
        "--- PROPOSAL START ---\n"
        f"{proposal_text[:20000]}\n"
        "--- PROPOSAL END ---\n"
    )


def _fact_check_system_prompt() -> str:
    return (
        "You are a fact-checking agent. Evaluate whether the given claim is verifiable "
        "and, to the best of your knowledge, accurate. Use the STRUCTURED_OUTPUT contract.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "VERDICT|VERIFIED|PARTIALLY_TRUE|UNVERIFIABLE|FALSE\n"
        "CONFIDENCE|high|medium|low\n"
        "STRUCTURED_OUTPUT_END\n"
        "Choose exactly one VERDICT and one CONFIDENCE level."
    )


def _fact_check_user_prompt(claim: dict[str, str]) -> str:
    return (
        f"Claim ID: {claim.get('id', '?')}\n"
        f"Tier: {claim.get('tier', '?')}\n\n"
        f"Claim text:\n{claim.get('text', '')}\n\n"
        "Evaluate this claim."
    )


def _assembly_system_prompt() -> str:
    return (
        "You are a proposal review synthesizer. Write a thorough review.md based on "
        "the extracted claims, weaknesses, and fact-check verdicts. Structure the report "
        "with: Executive Summary, Claims Analysis, Weaknesses by Dimension, Fact-Check "
        "Results, and Final Verdict. Be honest — do not invent new weaknesses.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "REPORT|<full markdown review body — use literal newlines inside the value>\n"
        "STRUCTURED_OUTPUT_END\n"
        "IMPORTANT: REPORT value is a single pipe-separated field; put the ENTIRE "
        "markdown (including headings) after the first pipe. Do not add extra pipe "
        "characters within the report — the parser uses the FIRST pipe as the separator."
    )


def _assembly_user_prompt(
    proposal_text: str,
    claims: list[dict[str, str]],
    weaknesses: list[dict[str, str]],
    fact_checks: list[dict[str, str]],
) -> str:
    return (
        f"Proposal length: {len(proposal_text)} chars\n"
        f"Claims ({len(claims)}):\n{json.dumps(claims, indent=2)}\n\n"
        f"Weaknesses ({len(weaknesses)}):\n{json.dumps(weaknesses, indent=2)}\n\n"
        f"Fact-check results ({len(fact_checks)}):\n{json.dumps(fact_checks, indent=2)}\n\n"
        "Write the comprehensive review."
    )


# --- helpers ---


def _parse_json_list(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass
    return []


def _termination_label(fact_checks: list[dict[str, str]]) -> str:
    """Derive label from verdict distribution."""
    if not fact_checks:
        return "insufficient_evidence_to_review"
    verdicts = [fc.get("verdict", "UNVERIFIABLE") for fc in fact_checks]
    verified_count = sum(1 for v in verdicts if v == "VERIFIED")
    false_count = sum(1 for v in verdicts if v == "FALSE")
    total = len(verdicts)
    if false_count > 0 or verified_count < total / 2:
        return "mixed_evidence"
    return "high_conviction_review"


def _fallback_report(
    claims: list[dict[str, str]],
    weaknesses: list[dict[str, str]],
    fact_checks: list[dict[str, str]],
) -> str:
    label = _termination_label(fact_checks)
    lines = [
        "# Proposal Review (fallback synthesis)",
        "",
        "The synthesizer produced no structured output.",
        "",
        f"**Termination label:** {label}",
        f"**Claims extracted:** {len(claims)}",
        f"**Weaknesses found:** {len(weaknesses)}",
        "",
        "## Weaknesses",
        "",
    ]
    for w in weaknesses:
        lines.append(
            f"- [{w.get('severity', '?')}] [{w.get('dimension', '?')}] "
            f"{w.get('title', '?')} — {w.get('scenario', '')}"
        )
    return "\n".join(lines)
