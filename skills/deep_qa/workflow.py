"""deep-qa-temporal: Temporal port of the deep-qa skill.

Phases (minimum-viable):
  0. Snapshot artifact into the run directory.
  1. Dimension/angle discovery (Sonnet, one call).
  2. QA rounds (N rounds × up to 4 parallel critics, Haiku).
  3. Synthesize report (Sonnet).
  4. Emit finding to INBOX + desktop notification.

Deferred to follow-up versions: independent severity judges (pass 1+2),
rationalization auditor, fact-verification (research type), coverage
enforcement for required categories.
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
class DeepQaInput:
    run_id: str
    artifact_path: str  # absolute path to artifact to QA
    artifact_type: str  # "doc" | "code" | "research" | "skill"
    inbox_path: str
    run_dir: str
    max_rounds: int = 3
    notify: bool = True


# Bounded parallelism: at most this many critics per round.
_MAX_CRITICS_PER_ROUND = 4


@workflow.defn(name="DeepQaWorkflow")
class DeepQaWorkflow:
    @workflow.run
    async def run(self, inp: DeepQaInput) -> str:
        artifact_snapshot = f"{inp.run_dir}/artifact.txt"
        report_path = f"{inp.run_dir}/qa-report.md"
        dim_prompt_path = f"{inp.run_dir}/dim-prompt.txt"
        synth_prompt_path = f"{inp.run_dir}/synth-prompt.txt"

        # Phase 0: snapshot artifact + write the dim-discovery prompt.
        artifact_text = await workflow.execute_activity(
            "read_text_file",
            inp.artifact_path,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=HAIKU_POLICY,
        )
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=artifact_snapshot, content=artifact_text),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=dim_prompt_path,
                content=_dim_discovery_user_prompt(
                    artifact_text=artifact_text, artifact_type=inp.artifact_type
                ),
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        # Phase 1: dimension discovery → list of angles
        dim_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="dim-discover",
                tier_name="SONNET",
                system_prompt=_dim_discovery_system_prompt(),
                user_prompt_path=dim_prompt_path,
                max_tokens=1024,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=180),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=SONNET_POLICY,
        )
        angles_raw = dim_result.get("ANGLES", "[]")
        angles = _parse_angles(angles_raw)

        all_defects: list[dict[str, str]] = []

        # Phase 2: QA rounds.
        rounds_run = 0
        for round_idx in range(inp.max_rounds):
            if not angles:
                break
            batch = angles[:_MAX_CRITICS_PER_ROUND]
            angles = angles[_MAX_CRITICS_PER_ROUND:]
            rounds_run += 1

            # Write per-angle critic prompt files.
            critic_prompt_paths: list[str] = []
            for i, angle in enumerate(batch):
                ppath = f"{inp.run_dir}/critic-r{round_idx}-{i}.txt"
                await workflow.execute_activity(
                    "write_artifact",
                    WriteArtifactInput(
                        path=ppath,
                        content=_critic_user_prompt(
                            artifact_text=artifact_text,
                            angle=angle,
                            artifact_type=inp.artifact_type,
                        ),
                    ),
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=HAIKU_POLICY,
                )
                critic_prompt_paths.append(ppath)

            # Spawn critics in parallel.
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

            for angle, result in zip(batch, critic_outputs):
                if isinstance(result, BaseException):
                    # Critic failure: fail-safe skip this angle; do NOT re-queue.
                    continue
                # Defects are encoded as a JSON array in the DEFECTS field.
                defects_raw = result.get("DEFECTS", "[]")
                for defect in _parse_defects(defects_raw):
                    defect.setdefault("source_angle", angle.get("id", ""))
                    all_defects.append(defect)

        # Phase 3: synthesize report.
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(
                path=synth_prompt_path,
                content=_synth_user_prompt(
                    artifact_type=inp.artifact_type,
                    defects=all_defects,
                    rounds_run=rounds_run,
                    max_rounds=inp.max_rounds,
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
        report_md = synth_result.get("REPORT", _fallback_report(all_defects, rounds_run))

        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=report_path, content=report_md),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        # Phase 4: emit finding.
        severity_count = _severity_tally(all_defects)
        summary = (
            f"{severity_count['critical']} critical, "
            f"{severity_count['major']} major, "
            f"{severity_count['minor']} minor defects across {rounds_run} rounds"
        )
        status = "DONE" if rounds_run > 0 else "EMPTY"
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="deep-qa",
                status=status,
                summary=summary,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{summary}\nReport: {report_path}"


# --- prompt templates (kept inline to avoid file-read activities for static text) ---


def _dim_discovery_system_prompt() -> str:
    return (
        "You are a QA dimension-discovery agent. Given an artifact, enumerate 4-8 "
        "independent QA angles that would find the most important defects. Each "
        "angle is a specific question a critic could try to answer. Be concrete — "
        "avoid vague angles like 'is this correct'. Respond using the "
        "STRUCTURED_OUTPUT contract.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        'ANGLES|[{"id":"a1","dimension":"<name>","question":"<one sentence>"}, ...]\n'
        "STRUCTURED_OUTPUT_END\n"
        "The ANGLES value must be valid JSON. Keep it compact."
    )


def _dim_discovery_user_prompt(artifact_text: str, artifact_type: str) -> str:
    return (
        f"Artifact type: {artifact_type}\n"
        f"Length: {len(artifact_text)} chars\n\n"
        "Generate 4-8 QA angles that cover different failure modes (correctness, "
        "edge cases, security/safety, maintainability, clarity, interop). Don't "
        "duplicate — each angle should attack a different dimension.\n\n"
        "--- ARTIFACT START ---\n"
        f"{artifact_text[:20000]}\n"
        "--- ARTIFACT END ---\n"
    )


def _critic_system_prompt() -> str:
    return (
        "You are a QA critic. Given an artifact and one specific angle, find "
        "real defects. A defect needs a concrete scenario (when does it fail?), "
        "root cause, and severity. Only flag real problems — don't pad.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        'DEFECTS|[{"id":"d1","title":"<short>","severity":"critical|major|minor",'
        '"dimension":"<from angle>","scenario":"<concrete trigger>","root_cause":"<why>"}, ...]\n'
        "STRUCTURED_OUTPUT_END\n"
        "If no real defects, emit DEFECTS|[]. Do not invent problems."
    )


def _critic_user_prompt(artifact_text: str, angle: dict[str, str], artifact_type: str) -> str:
    return (
        f"Angle: {angle.get('question', '')}\n"
        f"Dimension: {angle.get('dimension', '')}\n"
        f"Artifact type: {artifact_type}\n\n"
        "--- ARTIFACT START ---\n"
        f"{artifact_text[:20000]}\n"
        "--- ARTIFACT END ---\n"
    )


def _synth_system_prompt() -> str:
    return (
        "You are a QA report synthesizer. Given a list of defects, write a "
        "concise qa-report.md grouping by severity (critical → major → minor). "
        "Be honest — include an executive summary, and call out if there are no "
        "findings. Do not invent new defects; work from what the critics reported.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "REPORT|<full markdown report body here — use literal newlines inside the value>\n"
        "STRUCTURED_OUTPUT_END\n"
        "IMPORTANT: the REPORT value is a single pipe-separated field; put the ENTIRE "
        "markdown report (including headings) after the first pipe. Do not add extra "
        "pipe characters within the report — the parser uses the FIRST pipe as the "
        "key/value separator and preserves the rest verbatim."
    )


def _synth_user_prompt(
    artifact_type: str,
    defects: list[dict[str, str]],
    rounds_run: int,
    max_rounds: int,
) -> str:
    return (
        f"Artifact type: {artifact_type}\n"
        f"Rounds executed: {rounds_run} / {max_rounds}\n"
        f"Defects found ({len(defects)}):\n"
        f"{json.dumps(defects, indent=2)}\n\n"
        "Write the report."
    )


# --- helpers ---


def _parse_angles(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [a for a in parsed if isinstance(a, dict)]
    except json.JSONDecodeError:
        pass
    return []


def _parse_defects(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [d for d in parsed if isinstance(d, dict)]
    except json.JSONDecodeError:
        pass
    return []


def _severity_tally(defects: list[dict[str, str]]) -> dict[str, int]:
    counts = {"critical": 0, "major": 0, "minor": 0}
    for d in defects:
        sev = d.get("severity", "minor").lower()
        if sev in counts:
            counts[sev] += 1
    return counts


def _fallback_report(defects: list[dict[str, str]], rounds_run: int) -> str:
    counts = _severity_tally(defects)
    lines = [
        "# QA Report (fallback synthesis)",
        "",
        f"The synthesizer produced no structured output. Rounds run: {rounds_run}.",
        "",
        f"**Severity tally:** {counts['critical']} critical / {counts['major']} major / "
        f"{counts['minor']} minor.",
        "",
        "## Defects",
        "",
    ]
    for d in defects:
        lines.append(
            f"- [{d.get('severity', 'minor')}] {d.get('title', '?')} — "
            f"{d.get('scenario', '')}"
        )
    return "\n".join(lines)
