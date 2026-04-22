"""deep-debug-temporal workflow.

Minimum-viable port of deep-debug's hypothesis-driven investigation:
  1. Hypothesis generation (Sonnet, parallel × N)
  2. Independent judge (Haiku)
  3. Report synthesis (Sonnet)

Deferred: discriminating probes, rebuttal rounds, red-green-red demo loop,
architectural escalation to Opus after 3 failed fix attempts.
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
class DeepDebugInput:
    run_id: str
    symptom: str
    reproduction_command: str
    inbox_path: str
    run_dir: str
    num_hypotheses: int = 4
    notify: bool = True


@workflow.defn(name="DeepDebugWorkflow")
class DeepDebugWorkflow:
    @workflow.run
    async def run(self, inp: DeepDebugInput) -> str:
        report_path = f"{inp.run_dir}/debug-report.md"

        # Phase 1: parallel hypothesis generation.
        hyp_paths: list[str] = []
        for i in range(inp.num_hypotheses):
            p = f"{inp.run_dir}/hyp-prompt-{i}.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=p,
                    content=_hyp_user_prompt(inp.symptom, inp.reproduction_command, i),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            hyp_paths.append(p)

        hyp_results = await asyncio.gather(*[
            workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="hypothesis",
                    tier_name="SONNET",
                    system_prompt=_hyp_system_prompt(),
                    user_prompt_path=p,
                    max_tokens=1024,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=SONNET_POLICY,
            )
            for p in hyp_paths
        ], return_exceptions=True)

        hypotheses: list[dict[str, str]] = []
        for i, r in enumerate(hyp_results):
            if isinstance(r, BaseException):
                continue
            h = {
                "id": f"h{i}",
                "dimension": r.get("DIMENSION", "unknown"),
                "mechanism": r.get("MECHANISM", ""),
                "evidence_tier": r.get("EVIDENCE_TIER", "unknown"),
            }
            if h["mechanism"]:
                hypotheses.append(h)

        # Phase 2: single batched judge.
        judge_prompt = f"{inp.run_dir}/judge-prompt.txt"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=judge_prompt,
                content=_judge_user_prompt(inp.symptom, hypotheses),
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        judge_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="judge",
                tier_name="HAIKU",
                system_prompt=_judge_system_prompt(),
                user_prompt_path=judge_prompt,
                max_tokens=1024,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=HAIKU_POLICY,
        )
        verdicts = _parse_json_list(judge_result.get("VERDICTS", "[]"))

        # Phase 3: synthesize report.
        synth_prompt = f"{inp.run_dir}/synth-prompt.txt"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=synth_prompt,
                content=_synth_user_prompt(inp.symptom, hypotheses, verdicts),
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
                user_prompt_path=synth_prompt,
                max_tokens=4096,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=240),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        report_md = synth_result.get("REPORT", _fallback(hypotheses, verdicts))

        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=report_path, content=report_md),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        leading = next((v for v in verdicts if v.get("plausibility") == "leading"), None)
        status = "DONE"
        summary = (
            f"{len(hypotheses)} hypotheses judged; "
            f"leader={leading.get('hyp_id') if leading else 'none'}"
        )
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="deep-debug",
                status=status,
                summary=summary,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{summary}\nReport: {report_path}"


def _hyp_system_prompt() -> str:
    return (
        "You are a debugging hypothesis generator. Given a symptom and reproduction, "
        "propose ONE causal hypothesis on one orthogonal dimension (correctness, "
        "concurrency, environment, resource, ordering, dependency). Include a mechanism "
        "and an evidence tier (1=direct observation, 3=plausible mechanism, 5=speculative).\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "DIMENSION|<one of: correctness, concurrency, environment, resource, ordering, dependency>\n"
        "MECHANISM|<one sentence causal chain>\n"
        "EVIDENCE_TIER|1|2|3|4|5\n"
        "STRUCTURED_OUTPUT_END"
    )


def _hyp_user_prompt(symptom: str, repro: str, variant: int) -> str:
    angles = [
        "logic / boundary conditions",
        "concurrency / shared state",
        "environment / configuration",
        "resource / timing",
        "dependency / API contract",
    ]
    return (
        f"Symptom: {symptom}\n"
        f"Reproduction: {repro}\n"
        f"Focus on: {angles[variant % len(angles)]}\n"
    )


def _judge_system_prompt() -> str:
    return (
        "You are an independent hypothesis judge. Review each proposed hypothesis "
        "and classify its plausibility. Only one may be 'leading'.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        'VERDICTS|[{"hyp_id":"hN","plausibility":"leading|plausible|rejected","rationale":"<one line>"}, ...]\n'
        "STRUCTURED_OUTPUT_END"
    )


def _judge_user_prompt(symptom: str, hypotheses: list[dict[str, str]]) -> str:
    return f"Symptom: {symptom}\n\nHypotheses:\n{json.dumps(hypotheses, indent=2)}"


def _synth_system_prompt() -> str:
    return (
        "Write a debug-report.md given the symptom, hypotheses, and judge verdicts. "
        "Lead with the likely cause, list alternatives with why-rejected, and note "
        "next-step probes if no clear leader.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "REPORT|<full markdown>\n"
        "STRUCTURED_OUTPUT_END"
    )


def _synth_user_prompt(
    symptom: str,
    hypotheses: list[dict[str, str]],
    verdicts: list[dict[str, str]],
) -> str:
    return (
        f"Symptom: {symptom}\n\n"
        f"Hypotheses: {json.dumps(hypotheses, indent=2)}\n\n"
        f"Verdicts: {json.dumps(verdicts, indent=2)}"
    )


def _parse_json_list(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass
    return []


def _fallback(hypotheses: list[dict[str, str]], verdicts: list[dict[str, str]]) -> str:
    lines = ["# Debug Report (fallback)", "", "## Hypotheses"]
    for h in hypotheses:
        lines.append(f"- {h.get('dimension', '?')}: {h.get('mechanism', '')}")
    lines.extend(["", "## Verdicts"])
    for v in verdicts:
        lines.append(f"- {v.get('hyp_id', '?')}: {v.get('plausibility', '?')} — {v.get('rationale', '')}")
    return "\n".join(lines)
