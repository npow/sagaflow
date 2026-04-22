"""deep-design-temporal: Temporal port of the deep-design skill.

Phases (minimum-viable):
  a. Initial draft (Sonnet): from concept, produce v0-initial.md stored as
     a string in state. Emit STRUCTURED_OUTPUT SPEC|<markdown>.
  b. 1-2 rounds of parallel critique (Haiku × 4 critics): each returns
     FLAWS|<json array of {id,title,severity,dimension,scenario}>.
  c. Redesign (Sonnet): given spec + flaws, emit SPEC|<revised markdown>.
  d. Final synthesis (Sonnet): emit REPORT|<spec.md content>.
  e. Write final spec.md to run_dir, emit_finding to INBOX.
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


@dataclass(frozen=True)
class DeepDesignInput:
    run_id: str
    concept: str          # free-form description of the design to produce
    inbox_path: str
    run_dir: str
    max_rounds: int = 2
    notify: bool = True


# Max critics per critique round.
_MAX_CRITICS_PER_ROUND = 4


@workflow.defn(name="DeepDesignWorkflow")
class DeepDesignWorkflow:
    @workflow.run
    async def run(self, inp: DeepDesignInput) -> str:
        draft_prompt_path = f"{inp.run_dir}/draft-prompt.txt"
        spec_path = f"{inp.run_dir}/spec.md"
        synth_prompt_path = f"{inp.run_dir}/synth-prompt.txt"

        # Phase a: write draft prompt and call Sonnet for initial spec.
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=draft_prompt_path,
                content=_draft_user_prompt(concept=inp.concept),
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        draft_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="draft",
                tier_name="SONNET",
                system_prompt=_draft_system_prompt(),
                user_prompt_path=draft_prompt_path,
                max_tokens=4096,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=240),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        spec_md = draft_result.get("SPEC", _fallback_spec(inp.concept))

        all_flaws: list[dict[str, str]] = []

        # Phase b: critique rounds (Haiku × up to _MAX_CRITICS_PER_ROUND).
        for round_idx in range(max(1, min(inp.max_rounds, 2))):
            critic_prompt_paths: list[str] = []
            for i in range(_MAX_CRITICS_PER_ROUND):
                ppath = f"{inp.run_dir}/critic-r{round_idx}-{i}.txt"
                await workflow.execute_activity(
                    "write_artifact",
                    WriteArtifactInput(
                        path=ppath,
                        content=_critic_user_prompt(
                            spec_md=spec_md,
                            concept=inp.concept,
                            critic_index=i,
                        ),
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
                        system_prompt=_critic_system_prompt(),
                        user_prompt_path=ppath,
                        max_tokens=1024,
                        tools_needed=False,
                    ),
                    start_to_close_timeout=timedelta(seconds=180),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=HAIKU_POLICY,
                )
                for ppath in critic_prompt_paths
            ]
            critic_outputs = await asyncio.gather(*critic_coros, return_exceptions=True)

            for result in critic_outputs:
                if isinstance(result, BaseException):
                    continue
                flaws_raw = result.get("FLAWS", "[]")
                for flaw in _parse_flaws(flaws_raw):
                    all_flaws.append(flaw)

            if not all_flaws:
                # No flaws found — skip redesign round.
                continue

            # Phase c: redesign with Sonnet after each critique round.
            redesign_prompt_path = f"{inp.run_dir}/redesign-r{round_idx}.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=redesign_prompt_path,
                    content=_redesign_user_prompt(spec_md=spec_md, flaws=all_flaws),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            redesign_result = await workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="redesign",
                    tier_name="SONNET",
                    system_prompt=_redesign_system_prompt(),
                    user_prompt_path=redesign_prompt_path,
                    max_tokens=4096,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=240),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=SONNET_POLICY,
            )
            spec_md = redesign_result.get("SPEC", spec_md)

        # Phase d: final synthesis (Sonnet).
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=synth_prompt_path,
                content=_synth_user_prompt(
                    concept=inp.concept,
                    spec_md=spec_md,
                    flaws=all_flaws,
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
                system_prompt=_synth_system_prompt(),
                user_prompt_path=synth_prompt_path,
                max_tokens=4096,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=240),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        final_spec = synth_result.get("REPORT", spec_md)

        # Phase e: write spec.md and emit finding.
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=spec_path, content=final_spec),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        flaw_count = len(all_flaws)
        summary = (
            f"{flaw_count} flaw(s) surfaced across {max(1, min(inp.max_rounds, 2))} "
            f"critique round(s) — final spec at {spec_path}"
        )
        status = "DONE"
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="deep-design",
                status=status,
                summary=summary,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{summary}"


# --- prompt templates (inline to avoid file-read activities for static text) ---


def _draft_system_prompt() -> str:
    return (
        "You are a senior software architect. Given a design concept, produce a "
        "structured technical specification as Markdown. Include: overview, goals, "
        "non-goals, key components, interfaces/APIs, data model, failure modes, "
        "and open questions. Be concrete and opinionated — avoid vague hand-waving.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "SPEC|<full Markdown spec — use literal newlines inside the value>\n"
        "STRUCTURED_OUTPUT_END\n"
        "IMPORTANT: the SPEC value is a single pipe-separated field; put the ENTIRE "
        "Markdown spec after the first pipe. Do not add extra pipe characters within "
        "the spec — the parser uses the FIRST pipe as the key/value separator."
    )


def _draft_user_prompt(concept: str) -> str:
    return (
        f"Design concept:\n{concept}\n\n"
        "Produce a complete technical specification for this concept. "
        "Make it specific enough that an engineer could start implementation."
    )


def _critic_system_prompt() -> str:
    return (
        "You are a design critic reviewing a technical specification. Find real "
        "flaws: ambiguities, missing failure modes, scalability issues, security "
        "gaps, inconsistent interfaces, or unjustified assumptions. Be specific — "
        "cite the relevant part of the spec and explain the concrete problem.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        'FLAWS|[{"id":"f1","title":"<short label>","severity":"critical|major|minor",'
        '"dimension":"<category>","scenario":"<concrete trigger or situation>"}, ...]\n'
        "STRUCTURED_OUTPUT_END\n"
        "If no real flaws, emit FLAWS|[]. Do not invent problems."
    )


def _critic_user_prompt(spec_md: str, concept: str, critic_index: int) -> str:
    dimensions = [
        "scalability and performance",
        "failure modes and resilience",
        "security and data integrity",
        "API clarity and consistency",
    ]
    focus = dimensions[critic_index % len(dimensions)]
    return (
        f"Design concept: {concept}\n"
        f"Your focus dimension: {focus}\n\n"
        "--- SPEC START ---\n"
        f"{spec_md[:20000]}\n"
        "--- SPEC END ---\n\n"
        f"Review the spec above focusing on {focus}. Find real flaws only."
    )


def _redesign_system_prompt() -> str:
    return (
        "You are a senior software architect revising a technical specification "
        "based on critic feedback. Address each flaw: fix ambiguities, add missing "
        "sections, correct inconsistencies. Keep what is already good.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "SPEC|<full revised Markdown spec — use literal newlines inside the value>\n"
        "STRUCTURED_OUTPUT_END\n"
        "IMPORTANT: the SPEC value is a single pipe-separated field; put the ENTIRE "
        "revised Markdown spec after the first pipe."
    )


def _redesign_user_prompt(spec_md: str, flaws: list[dict[str, str]]) -> str:
    return (
        "--- CURRENT SPEC START ---\n"
        f"{spec_md[:20000]}\n"
        "--- CURRENT SPEC END ---\n\n"
        f"Flaws to address ({len(flaws)}):\n"
        f"{json.dumps(flaws, indent=2)}\n\n"
        "Produce the revised spec addressing all listed flaws."
    )


def _synth_system_prompt() -> str:
    return (
        "You are a technical writer producing the final version of a design "
        "specification. Given the refined spec and a list of flaws that were "
        "addressed, produce a clean, polished spec.md. Add a revision summary "
        "section at the end listing what was improved.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "REPORT|<full final Markdown spec — use literal newlines inside the value>\n"
        "STRUCTURED_OUTPUT_END\n"
        "IMPORTANT: the REPORT value is a single pipe-separated field; put the ENTIRE "
        "Markdown document after the first pipe."
    )


def _synth_user_prompt(
    concept: str,
    spec_md: str,
    flaws: list[dict[str, str]],
) -> str:
    return (
        f"Original concept: {concept}\n\n"
        "--- REFINED SPEC START ---\n"
        f"{spec_md[:20000]}\n"
        "--- REFINED SPEC END ---\n\n"
        f"Flaws addressed ({len(flaws)}):\n"
        f"{json.dumps(flaws, indent=2)}\n\n"
        "Produce the final polished spec.md."
    )


# --- helpers ---


def _parse_flaws(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [f for f in parsed if isinstance(f, dict)]
    except json.JSONDecodeError:
        pass
    return []


def _fallback_spec(concept: str) -> str:
    return (
        f"# Design Specification (fallback draft)\n\n"
        f"**Concept:** {concept}\n\n"
        "The initial draft agent produced no structured output. "
        "A human should complete this specification.\n\n"
        "## Open Questions\n\n"
        "- What are the primary goals and non-goals?\n"
        "- What components are needed?\n"
        "- What are the key interfaces?\n"
    )
