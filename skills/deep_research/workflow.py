"""deep-research-temporal: dimension-expanded research with synthesis.

MVP port:
  1. Direction discovery (Sonnet) — enumerates WHO/WHAT/HOW/WHERE/WHEN angles.
  2. Parallel researchers (Sonnet) — findings per direction.
  3. Synthesis (Sonnet) — research-report.md.

Deferred: fact verifier, cross-cut dimensions, translation round-trip,
novelty classification, vocabulary bootstrap.
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
class DeepResearchInput:
    run_id: str
    seed: str
    inbox_path: str
    run_dir: str
    max_rounds: int = 1
    max_directions: int = 5
    notify: bool = True


@workflow.defn(name="DeepResearchWorkflow")
class DeepResearchWorkflow:
    @workflow.run
    async def run(self, inp: DeepResearchInput) -> str:
        report_path = f"{inp.run_dir}/research-report.md"

        # Phase 1: direction discovery.
        dim_prompt = f"{inp.run_dir}/dim-prompt.txt"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=dim_prompt,
                content=f"Seed: {inp.seed}\n\nGenerate {inp.max_directions} research directions "
                        f"across orthogonal dimensions (WHO/WHAT/HOW/WHERE/WHEN/WHY/LIMITS).",
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        dim_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="dim-discover",
                tier_name="SONNET",
                system_prompt=(
                    "You generate research directions. STRUCTURED_OUTPUT_START\n"
                    'DIRECTIONS|[{"id":"d1","dimension":"HOW","question":"<specific>"}, ...]\n'
                    "STRUCTURED_OUTPUT_END"
                ),
                user_prompt_path=dim_prompt,
                max_tokens=1024,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=180),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        directions = _parse_json_list(dim_result.get("DIRECTIONS", "[]"))[:inp.max_directions]

        # Phase 2: research per direction in parallel.
        research_prompts: list[str] = []
        for d in directions:
            p = f"{inp.run_dir}/research-{d.get('id', 'x')}.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=p,
                    content=f"Seed: {inp.seed}\nDirection ({d.get('dimension', '?')}): "
                            f"{d.get('question', '?')}\n\nResearch this direction.",
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            research_prompts.append(p)

        research_coros = [
            workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="researcher",
                    tier_name="SONNET",
                    system_prompt=(
                        "You research one direction. Return findings + 3-5 key sources. "
                        "STRUCTURED_OUTPUT_START\n"
                        'FINDINGS|<prose summary>\nSOURCES|["title1", "title2", ...]\n'
                        "STRUCTURED_OUTPUT_END"
                    ),
                    user_prompt_path=p,
                    max_tokens=2048,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=240),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=SONNET_POLICY,
            )
            for p in research_prompts
        ]
        research_results = await asyncio.gather(*research_coros, return_exceptions=True)

        findings: list[dict[str, str]] = []
        for d, r in zip(directions, research_results):
            if isinstance(r, BaseException):
                continue
            findings.append({
                "id": d.get("id", "?"),
                "dimension": d.get("dimension", "?"),
                "question": d.get("question", "?"),
                "findings": r.get("FINDINGS", ""),
                "sources": r.get("SOURCES", "[]"),
            })

        # Phase 3: synthesis.
        synth_prompt = f"{inp.run_dir}/synth-prompt.txt"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=synth_prompt,
                content=f"Seed: {inp.seed}\n\nFindings: {json.dumps(findings, indent=2)}",
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        synth_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="synth",
                tier_name="SONNET",
                system_prompt=(
                    "Write research-report.md. Executive summary, findings per direction, "
                    "sources, gaps. STRUCTURED_OUTPUT_START\n"
                    "REPORT|<full markdown>\n"
                    "STRUCTURED_OUTPUT_END"
                ),
                user_prompt_path=synth_prompt,
                max_tokens=4096,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=240),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        report_md = synth_result.get("REPORT", _fallback(inp.seed, findings))
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=report_path, content=report_md),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        summary = f"{len(findings)} directions researched across {len(set(f['dimension'] for f in findings))} dimensions"
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="deep-research",
                status="DONE",
                summary=summary,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{summary}\nReport: {report_path}"


def _parse_json_list(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass
    return []


def _fallback(seed: str, findings: list[dict[str, str]]) -> str:
    lines = [f"# Research Report: {seed}", "", "## Findings"]
    for f in findings:
        lines.append(f"### {f.get('dimension', '?')}: {f.get('question', '?')}")
        lines.append(f.get('findings', ''))
        lines.append("")
    return "\n".join(lines)
