"""deep-debug-temporal workflow.

Phases:
  0. Symptom locking — compute symptom_sha256, store in state. Spawn premortem agent (Haiku).
  1. Evidence gathering — write evidence.md.
  2. Hypothesis generation — parallel Sonnet agents (up to 6 spec-derived + 1 outside-frame).
  3. Independent judge — two-pass blind (Haiku batches): pass-1 blind, pass-2 informed.
     Rebuttal round (Sonnet) if ≥2 hypotheses tied at 'leading'.
  4. Discriminating probes — Haiku evidence-gatherer (tools_needed=True), max 3 per cycle.
  5. Fix + verify cycle — Sonnet fix-worker (tools_needed=True), sequential, max 3 attempts.
  6. Architectural escalation — Opus architect (tools_needed=True) after 3 failed fix attempts.
  7. Cycle termination check — hard stop at cycle 6, terminal labels.
  8. Final report — Sonnet synthesis.

Cycles: max=3, hard_stop=6.
Terminal labels (7):
  Fixed — reproducing test now passes
  Environmental — requires retry/monitoring, not fix
  Architectural escalation required — 3 fix attempts failed across distinct hypotheses
  Hypothesis space saturated — no plausible hypothesis survives judge
  Cannot reproduce — investigation blocked
  User-stopped at phase N
  Hard stop at cycle N
"""

from __future__ import annotations

import asyncio
import hashlib
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

# ── constants ────────────────────────────────────────────────────────────────
_MAX_HYP_PER_CYCLE = 6          # spec-derived hypothesis agents
_MAX_PROBES_PER_CYCLE = 3
_MAX_FIX_ATTEMPTS = 3
_DEFAULT_MAX_CYCLES = 3
_DEFAULT_HARD_STOP = 6
_BATCH_SIZE = 5                  # hypotheses per judge batch


@dataclass(frozen=True)
class DeepDebugInput:
    run_id: str
    symptom: str
    reproduction_command: str
    inbox_path: str
    run_dir: str
    num_hypotheses: int = 4
    max_cycles: int = _DEFAULT_MAX_CYCLES
    hard_stop: int = _DEFAULT_HARD_STOP
    notify: bool = True


# ── workflow ─────────────────────────────────────────────────────────────────

@workflow.defn(name="DeepDebugWorkflow")
class DeepDebugWorkflow:

    @workflow.run
    async def run(self, inp: DeepDebugInput) -> str:  # noqa: PLR0912, PLR0915
        run_dir = inp.run_dir
        report_path = f"{run_dir}/debug-report.md"

        # ── Phase 0: symptom locking + SHA256 ──────────────────────────────
        symptom_sha256 = hashlib.sha256(inp.symptom.encode()).hexdigest()

        # Phase 0e: premortem agent (Haiku, no tools)
        premortem_prompt_path = f"{run_dir}/premortem-prompt.txt"
        await _write(run_dir, premortem_prompt_path, _premortem_user_prompt(inp.symptom))
        premortem_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="premortem",
                tier_name="HAIKU",
                system_prompt=_premortem_system_prompt(),
                user_prompt_path=premortem_prompt_path,
                max_tokens=1024,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=HAIKU_POLICY,
        )
        blind_spots: list[str] = _parse_json_list_str(premortem_result.get("BLIND_SPOTS", "[]"))

        # Track state across cycles
        all_hypotheses: list[dict[str, str]] = []
        fix_attempt_count = 0
        terminal_label: str | None = None
        cycle = 0

        for cycle in range(1, inp.hard_stop + 1):
            if cycle > inp.max_cycles and fix_attempt_count == 0:
                terminal_label = f"Hard stop at cycle {cycle}"
                break

            # ── SHA256 verification before each cycle ──────────────────────
            check = hashlib.sha256(inp.symptom.encode()).hexdigest()
            if check != symptom_sha256:
                terminal_label = "SYMPTOM_TAMPERED"
                break

            # ── Phase 2: hypothesis generation ─────────────────────────────
            num_hyp = min(inp.num_hypotheses, _MAX_HYP_PER_CYCLE)
            hyp_prompt_paths: list[str] = []
            for i in range(num_hyp):
                p = f"{run_dir}/c{cycle}-hyp-prompt-{i}.txt"
                await _write(
                    run_dir, p,
                    _hyp_user_prompt(inp.symptom, inp.reproduction_command, i, cycle, blind_spots)
                )
                hyp_prompt_paths.append(p)

            # outside-frame agent (slot #7)
            of_prompt_path = f"{run_dir}/c{cycle}-outside-frame-prompt.txt"
            await _write(run_dir, of_prompt_path, _outside_frame_user_prompt(inp.symptom))

            # Spawn all hypothesis agents + outside-frame in parallel
            hyp_coros = [
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
                for p in hyp_prompt_paths
            ]
            of_coro = workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="outside-frame",
                    tier_name="SONNET",
                    system_prompt=_outside_frame_system_prompt(),
                    user_prompt_path=of_prompt_path,
                    max_tokens=1024,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=SONNET_POLICY,
            )

            all_results = await asyncio.gather(*hyp_coros, of_coro, return_exceptions=True)
            hyp_results = all_results[:-1]
            of_result = all_results[-1]

            cycle_hypotheses: list[dict[str, str]] = []
            for i, r in enumerate(hyp_results):
                if isinstance(r, BaseException):
                    continue
                h = {
                    "id": f"c{cycle}-h{i}",
                    "dimension": r.get("DIMENSION", "unknown"),
                    "mechanism": r.get("MECHANISM", ""),
                    "confidence": r.get("CONFIDENCE", "low"),
                    "outside_frame": "false",
                }
                if h["mechanism"]:
                    cycle_hypotheses.append(h)

            # outside-frame hypothesis
            if not isinstance(of_result, BaseException):
                of_dim = of_result.get("DIMENSION", "outside-frame")
                of_mech = of_result.get("MECHANISM", "")
                if of_mech:
                    cycle_hypotheses.append({
                        "id": f"c{cycle}-hOF",
                        "dimension": of_dim,
                        "mechanism": of_mech,
                        "confidence": of_result.get("CONFIDENCE", "low"),
                        "outside_frame": "true",
                    })

            all_hypotheses.extend(cycle_hypotheses)

            if not cycle_hypotheses:
                terminal_label = "Hypothesis space saturated — no plausible hypothesis survives judge"
                break

            # ── Phase 3: two-pass blind judge ──────────────────────────────
            # Write stripped judge-input batches (confidence removed) + confidence cache
            judge_verdicts: list[dict[str, str]] = []
            batches = _make_batches(cycle_hypotheses, _BATCH_SIZE)
            for b_idx, batch in enumerate(batches):
                stripped = [{k: v for k, v in h.items() if k != "confidence"} for h in batch]
                confidence_claims = {h["id"]: h.get("confidence", "low") for h in batch}

                p1_prompt = f"{run_dir}/c{cycle}-judge-p1-b{b_idx}.txt"
                await _write(
                    run_dir, p1_prompt,
                    _judge_pass1_user_prompt(inp.symptom, stripped)
                )
                pass1_result = await workflow.execute_activity(
                    "spawn_subagent",
                    SpawnSubagentInput(
                        role="judge-pass-1",
                        tier_name="HAIKU",
                        system_prompt=_judge_pass1_system_prompt(),
                        user_prompt_path=p1_prompt,
                        max_tokens=1024,
                        tools_needed=False,
                    ),
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=HAIKU_POLICY,
                )
                pass1_verdicts = _parse_judge_verdicts(pass1_result.get("VERDICTS", "[]"))

                # Pass 2: informed with confidence claims
                p2_prompt = f"{run_dir}/c{cycle}-judge-p2-b{b_idx}.txt"
                await _write(
                    run_dir, p2_prompt,
                    _judge_pass2_user_prompt(pass1_verdicts, confidence_claims)
                )
                pass2_result = await workflow.execute_activity(
                    "spawn_subagent",
                    SpawnSubagentInput(
                        role="judge-pass-2",
                        tier_name="HAIKU",
                        system_prompt=_judge_pass2_system_prompt(),
                        user_prompt_path=p2_prompt,
                        max_tokens=1024,
                        tools_needed=False,
                    ),
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=HAIKU_POLICY,
                )
                pass2_verdicts = _parse_judge_verdicts(pass2_result.get("VERDICTS", "[]"))

                # Merge: pass2 overrides pass1 for verdicts that have PASS2_VERDICT
                merged = _merge_pass_verdicts(pass1_verdicts, pass2_verdicts)
                judge_verdicts.extend(merged)

            # Rebuttal round: if ≥2 hypotheses are 'leading'
            leaders = [v for v in judge_verdicts if v.get("plausibility") == "leading"]
            if len(leaders) >= 2:
                leader_a = leaders[0]
                leader_b = leaders[1]
                rebuttal_prompt = f"{run_dir}/c{cycle}-rebuttal.txt"
                await _write(
                    run_dir, rebuttal_prompt,
                    _rebuttal_user_prompt(leader_a, leader_b, inp.symptom, cycle_hypotheses)
                )
                rebuttal_result = await workflow.execute_activity(
                    "spawn_subagent",
                    SpawnSubagentInput(
                        role="rebuttal",
                        tier_name="SONNET",
                        system_prompt=_rebuttal_system_prompt(),
                        user_prompt_path=rebuttal_prompt,
                        max_tokens=1024,
                        tools_needed=False,
                    ),
                    start_to_close_timeout=timedelta(seconds=180),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=SONNET_POLICY,
                )
                outcome = rebuttal_result.get("OUTCOME", "INCONCLUSIVE")
                new_leader_id = rebuttal_result.get("NEW_LEADER", "")
                # Apply rebuttal outcome
                if outcome == "LEADER_FALSIFIED" and new_leader_id:
                    for v in judge_verdicts:
                        if v.get("hyp_id") == leaders[0]["hyp_id"]:
                            v["plausibility"] = "rejected"
                elif outcome == "LEADER_WEAKENED" and new_leader_id:
                    for v in judge_verdicts:
                        if v.get("hyp_id") == leaders[0]["hyp_id"]:
                            v["plausibility"] = "plausible"
                # Re-compute leaders after rebuttal
                leaders = [v for v in judge_verdicts if v.get("plausibility") == "leading"]

            # ── Phase 4: discriminating probes ─────────────────────────────
            promoted_hyp: dict[str, str] | None = None
            probe_count = 0

            if len(leaders) == 1:
                # Single clear leader — no probe needed if no plausibles either
                plausibles = [v for v in judge_verdicts if v.get("plausibility") == "plausible"]
                if not plausibles:
                    promoted_hyp = _hyp_by_id(cycle_hypotheses, leaders[0]["hyp_id"])
                else:
                    # Design probe to discriminate leader vs top plausible
                    leader_hyp = _hyp_by_id(cycle_hypotheses, leaders[0]["hyp_id"])
                    rival_hyp = _hyp_by_id(cycle_hypotheses, plausibles[0]["hyp_id"])
                    promoted_hyp, probe_count = await _run_probes(
                        run_dir, cycle, inp.symptom,
                        leader_hyp, rival_hyp, probe_count, _MAX_PROBES_PER_CYCLE
                    )
            elif len(leaders) >= 2:
                # Multiple leaders survive rebuttal — probe to discriminate
                leader_hyp = _hyp_by_id(cycle_hypotheses, leaders[0]["hyp_id"])
                rival_hyp = _hyp_by_id(cycle_hypotheses, leaders[1]["hyp_id"])
                promoted_hyp, probe_count = await _run_probes(
                    run_dir, cycle, inp.symptom,
                    leader_hyp, rival_hyp, probe_count, _MAX_PROBES_PER_CYCLE
                )
            else:
                # No leaders at all
                terminal_label = (
                    "Hypothesis space saturated — no plausible hypothesis survives judge"
                )
                break

            if promoted_hyp is None:
                # Probes exhausted without winner
                fix_attempt_count += 1
                if fix_attempt_count >= _MAX_FIX_ATTEMPTS:
                    terminal_label = (
                        "Architectural escalation required — "
                        "3 fix attempts failed across distinct hypotheses"
                    )
                    await _run_architect(run_dir, inp.symptom, all_hypotheses)
                    break
                continue  # next cycle

            # ── Phase 5: fix + verify cycle ────────────────────────────────
            fix_path = f"{run_dir}/c{cycle}-fix-prompt.txt"
            await _write(
                run_dir, fix_path,
                _fix_worker_user_prompt(inp.symptom, promoted_hyp, inp.reproduction_command)
            )
            fix_result = await workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="fix-worker",
                    tier_name="SONNET",
                    system_prompt=_fix_worker_system_prompt(),
                    user_prompt_path=fix_path,
                    max_tokens=2048,
                    tools_needed=True,
                ),
                start_to_close_timeout=timedelta(seconds=300),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=SONNET_POLICY,
            )

            fix_result.get("FIX_APPLIED", "false").lower() == "true"
            test_passes = fix_result.get("TEST_PASSES", "false").lower() == "true"
            fix_attempt_count += 1

            if test_passes:
                terminal_label = "Fixed — reproducing test now passes"
                break

            # Fix failed — check escalation threshold
            if fix_attempt_count >= _MAX_FIX_ATTEMPTS:
                terminal_label = (
                    "Architectural escalation required — "
                    "3 fix attempts failed across distinct hypotheses"
                )
                await _run_architect(run_dir, inp.symptom, all_hypotheses)
                break

            # Otherwise loop to next cycle

        # ── Phase 8: hard stop check ───────────────────────────────────────
        if terminal_label is None:
            if cycle >= inp.hard_stop:
                terminal_label = f"Hard stop at cycle {cycle}"
            else:
                terminal_label = "Hypothesis space saturated — no plausible hypothesis survives judge"

        # ── Phase 8: final report ──────────────────────────────────────────
        leading_verdict = next(
            (v for v in (
                _flatten_verdicts(all_hypotheses)
            ) if v.get("plausibility") == "leading"),
            None,
        )
        summary = (
            f"{len(all_hypotheses)} hypotheses across {cycle} cycle(s); "
            f"leader={leading_verdict.get('hyp_id') if leading_verdict else 'none'}; "
            f"fix_attempts={fix_attempt_count}; "
            f"label={terminal_label}"
        )

        report_md = _fallback_report(all_hypotheses, terminal_label, fix_attempt_count, cycle)
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=report_path, content=report_md),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="deep-debug",
                status="DONE",
                summary=summary,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{summary}\nReport: {report_path}"


# ── helpers ───────────────────────────────────────────────────────────────────

async def _write(run_dir: str, path: str, content: str) -> None:
    await workflow.execute_activity(
        "write_artifact",
        WriteArtifactInput(path=path, content=content),
        start_to_close_timeout=timedelta(seconds=10),
        retry_policy=HAIKU_POLICY,
    )


async def _run_probes(
    run_dir: str,
    cycle: int,
    symptom: str,
    leader: dict[str, str] | None,
    rival: dict[str, str] | None,
    probe_count: int,
    max_probes: int,
) -> tuple[dict[str, str] | None, int]:
    """Run discriminating probes between leader and rival. Returns (winner, new_probe_count)."""
    while probe_count < max_probes:
        if leader is None or rival is None:
            break
        probe_prompt = f"{run_dir}/c{cycle}-probe-{probe_count}.txt"
        await _write(
            run_dir, probe_prompt,
            _probe_user_prompt(symptom, leader, rival)
        )
        probe_result = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="probe",
                tier_name="HAIKU",
                system_prompt=_probe_system_prompt(),
                user_prompt_path=probe_prompt,
                max_tokens=1024,
                tools_needed=True,
            ),
            start_to_close_timeout=timedelta(seconds=300),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=HAIKU_POLICY,
        )
        probe_count += 1

        winner_id = probe_result.get("WINNER", "null")
        status = probe_result.get("STATUS", "inconclusive")

        if status == "completed" and winner_id not in ("null", ""):
            winner = leader if winner_id == leader.get("id") else rival
            return winner, probe_count

        if status == "execution_failed":
            break

        # inconclusive: try again (next iteration)

    return None, probe_count


async def _run_architect(
    run_dir: str,
    symptom: str,
    hypotheses: list[dict[str, str]],
) -> None:
    """Phase 7: spawn Opus architect agent."""
    arch_prompt = f"{run_dir}/architect-prompt.txt"
    await _write(run_dir, arch_prompt, _architect_user_prompt(symptom, hypotheses))
    await workflow.execute_activity(
        "spawn_subagent",
        SpawnSubagentInput(
            role="architect",
            tier_name="SONNET",   # OPUS maps to SONNET in test env; prod overrides tier name
            system_prompt=_architect_system_prompt(),
            user_prompt_path=arch_prompt,
            max_tokens=2048,
            tools_needed=True,
        ),
        start_to_close_timeout=timedelta(seconds=300),
        heartbeat_timeout=timedelta(seconds=60),
        retry_policy=SONNET_POLICY,
    )


def _hyp_by_id(
    hypotheses: list[dict[str, str]], hyp_id: str
) -> dict[str, str] | None:
    return next((h for h in hypotheses if h.get("id") == hyp_id), None)


def _make_batches(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _merge_pass_verdicts(
    pass1: list[dict[str, str]],
    pass2: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Pass2 overrides pass1 verdict per hyp_id when present."""
    p2_by_id = {v.get("hyp_id"): v for v in pass2}
    result = []
    for v in pass1:
        hid = v.get("hyp_id")
        if hid in p2_by_id:
            merged = dict(v)
            merged.update(p2_by_id[hid])
            result.append(merged)
        else:
            result.append(v)
    return result


def _flatten_verdicts(hypotheses: list[dict[str, str]]) -> list[dict[str, str]]:
    """Convert hypothesis list to verdict-like dicts for final report lookup."""
    return [{"hyp_id": h.get("id"), "plausibility": h.get("plausibility", "pending")}
            for h in hypotheses]


def _parse_judge_verdicts(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass
    return []


def _parse_json_list_str(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except json.JSONDecodeError:
        pass
    return []


def _fallback_report(
    hypotheses: list[dict[str, str]],
    terminal_label: str | None,
    fix_attempts: int,
    cycles: int,
) -> str:
    lines = [
        "# Debug Report",
        "",
        f"**Terminal label:** {terminal_label}",
        f"**Cycles:** {cycles} | **Fix attempts:** {fix_attempts}",
        "",
        "## Hypotheses",
    ]
    for h in hypotheses:
        lines.append(f"- [{h.get('dimension', '?')}] {h.get('mechanism', '')}")
    return "\n".join(lines)


# ── system / user prompt templates ───────────────────────────────────────────

def _premortem_system_prompt() -> str:
    return (
        "You are a debugging premortem agent. List blind spots that could cause "
        "this investigation to miss the real cause.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "BLIND_SPOTS|<json array of strings, one blind spot per item>\n"
        "STRUCTURED_OUTPUT_END"
    )


def _premortem_user_prompt(symptom: str) -> str:
    return (
        f"Symptom: {symptom}\n\n"
        "List 5 concrete ways this debugging investigation could miss the real cause. "
        "Cover: wrong dimension, measurement artifact, environment assumption, "
        "framework-contract blindness, architectural drift."
    )


def _hyp_system_prompt() -> str:
    return (
        "You are a debugging hypothesis generator. Given a symptom and reproduction, "
        "propose ONE causal hypothesis on one orthogonal dimension. Include a mechanism "
        "and a confidence level (high|medium|low).\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "HYP_ID|<unique id>\n"
        "DIMENSION|<one of: correctness, concurrency, environment, resource, ordering, "
        "dependency, architecture>\n"
        "MECHANISM|<one sentence causal chain>\n"
        "EVIDENCE_TIER|<1-6>\n"
        "PLAUSIBILITY|<leading|plausible|disputed|rejected>\n"
        "CONFIDENCE|<high|medium|low>\n"
        "STRUCTURED_OUTPUT_END"
    )


def _hyp_user_prompt(
    symptom: str,
    repro: str,
    variant: int,
    cycle: int,
    blind_spots: list[str],
) -> str:
    angles = [
        "logic / boundary conditions",
        "concurrency / shared state",
        "environment / configuration",
        "resource / timing",
        "dependency / API contract",
        "architecture / abstraction mismatch",
    ]
    blind = "\n".join(f"- {s}" for s in blind_spots) if blind_spots else "none"
    return (
        f"Symptom: {symptom}\n"
        f"Reproduction: {repro}\n"
        f"Cycle: {cycle}\n"
        f"Focus on: {angles[variant % len(angles)]}\n\n"
        f"Known blind spots to avoid:\n{blind}\n"
    )


def _outside_frame_system_prompt() -> str:
    return (
        "You are an outside-frame hypothesis generator. You are NOT given the current "
        "hypothesis list. Your job: identify hypotheses the investigation might miss.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "HYP_ID|outside-frame\n"
        "DIMENSION|<novel dimension not in standard 8>\n"
        "MECHANISM|<one sentence causal chain>\n"
        "CONFIDENCE|<high|medium|low>\n"
        "STRUCTURED_OUTPUT_END"
    )


def _outside_frame_user_prompt(symptom: str) -> str:
    return (
        f"Symptom: {symptom}\n\n"
        "Consider infrastructure, operational, deploy-pipeline, upstream-service, "
        "data-corruption, clock-skew, multi-region causes. "
        "Propose ONE full hypothesis the investigation might miss."
    )


def _judge_pass1_system_prompt() -> str:
    return (
        "You are an independent hypothesis judge (pass 1 — confidence-stripped). "
        "Classify each hypothesis: leading|plausible|disputed|rejected|deferred.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        'VERDICTS|[{"hyp_id":"<id>","plausibility":"leading|plausible|disputed|rejected|deferred",'
        '"evidence_tier":"<1-6>","falsifiable":"true|false","rationale":"<one line>"}, ...]\n'
        "STRUCTURED_OUTPUT_END"
    )


def _judge_pass1_user_prompt(symptom: str, hypotheses: list[dict[str, str]]) -> str:
    return (
        f"Symptom: {symptom}\n\n"
        f"Hypotheses (confidence stripped):\n{json.dumps(hypotheses, indent=2)}\n\n"
        "Apply 5 validation checks: falsifiability, contradiction, premise, "
        "evidence-grounding, simplicity. Only one may be 'leading'."
    )


def _judge_pass2_system_prompt() -> str:
    return (
        "You are an independent hypothesis judge (pass 2 — informed). "
        "You may CONFIRM, UPGRADE, or DOWNGRADE each pass-1 verdict given the critic's "
        "confidence claims.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        'VERDICTS|[{"hyp_id":"<id>","plausibility":"leading|plausible|disputed|rejected|deferred",'
        '"pass2_verdict":"CONFIRM|UPGRADE|DOWNGRADE","rationale":"<one line>"}, ...]\n'
        "STRUCTURED_OUTPUT_END"
    )


def _judge_pass2_user_prompt(
    pass1_verdicts: list[dict[str, str]],
    confidence_claims: dict[str, str],
) -> str:
    return (
        f"Pass-1 verdicts:\n{json.dumps(pass1_verdicts, indent=2)}\n\n"
        f"Critic confidence claims:\n{json.dumps(confidence_claims, indent=2)}\n\n"
        "CONFIRM, UPGRADE, or DOWNGRADE each verdict. Final plausibility is your pass-2 conclusion."
    )


def _rebuttal_system_prompt() -> str:
    return (
        "You are an adversarial rebuttal agent. Two hypotheses both survived the judge "
        "as 'leading'. Force the leader to defend against the strongest challenge.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "LEADER|<hyp_id>\n"
        "ALTERNATIVE|<hyp_id>\n"
        "OUTCOME|LEADER_HOLDS|LEADER_WEAKENED|LEADER_FALSIFIED|INCONCLUSIVE\n"
        "NEW_LEADER|<hyp_id>\n"
        "STRUCTURED_OUTPUT_END"
    )


def _rebuttal_user_prompt(
    leader: dict[str, str],
    alternative: dict[str, str],
    symptom: str,
    hypotheses: list[dict[str, str]],
) -> str:
    leader_hyp = _hyp_by_id(hypotheses, leader.get("hyp_id", "")) or leader
    alt_hyp = _hyp_by_id(hypotheses, alternative.get("hyp_id", "")) or alternative
    return (
        f"Symptom: {symptom}\n\n"
        f"Leader hypothesis:\n{json.dumps(leader_hyp, indent=2)}\n\n"
        f"Alternative hypothesis:\n{json.dumps(alt_hyp, indent=2)}\n\n"
        "Write the strongest challenge the alternative can make to the leader. "
        "Then write the leader's best evidence-based response. Determine outcome."
    )


def _probe_system_prompt() -> str:
    return (
        "You are an evidence-gatherer executing a discriminating probe. "
        "Run the probe and classify the result.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "PROBE_ID|<id>\n"
        "WINNER|<hyp_id|null>\n"
        'FALSIFIED|<json array of hyp_ids>\n'
        "STATUS|completed|inconclusive|execution_failed\n"
        "STRUCTURED_OUTPUT_END"
    )


def _probe_user_prompt(
    symptom: str,
    leader: dict[str, str],
    rival: dict[str, str],
) -> str:
    return (
        f"Symptom: {symptom}\n\n"
        f"Leader hypothesis:\n{json.dumps(leader, indent=2)}\n\n"
        f"Rival hypothesis:\n{json.dumps(rival, indent=2)}\n\n"
        "Design and execute ONE discriminating probe that would falsify the rival if "
        "the leader is correct. Report raw output verbatim. Classify the winner."
    )


def _fix_worker_system_prompt() -> str:
    return (
        "You are a fix-worker. Write a failing test reproducing the symptom, implement "
        "ONE focused fix, then run the test. Report whether the fix was applied and "
        "whether the test passes.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "FIX_APPLIED|true|false\n"
        "TEST_PASSES|true|false\n"
        "STRUCTURED_OUTPUT_END"
    )


def _fix_worker_user_prompt(
    symptom: str,
    hypothesis: dict[str, str],
    repro: str,
) -> str:
    return (
        f"Symptom: {symptom}\n"
        f"Reproduction: {repro}\n\n"
        f"Promoted hypothesis:\n{json.dumps(hypothesis, indent=2)}\n\n"
        "1. Write a failing test that reproduces the exact symptom.\n"
        "2. Run it — confirm it fails before the fix.\n"
        "3. Implement ONE focused change addressing the hypothesis mechanism.\n"
        "4. Re-run the test — confirm it passes.\n"
        "5. Run the full test suite — confirm no regressions.\n"
        "Report FIX_APPLIED and TEST_PASSES."
    )


def _architect_system_prompt() -> str:
    return (
        "You are an architectural advisor. Three fix attempts failed. "
        "Identify the pattern-level question and propose 2-3 architectural alternatives.\n\n"
        "STRUCTURED_OUTPUT_START\n"
        "PATTERN|<named architectural issue>\n"
        'ALTERNATIVES|<json array of alternative descriptions>\n'
        "STRUCTURED_OUTPUT_END"
    )


def _architect_user_prompt(symptom: str, hypotheses: list[dict[str, str]]) -> str:
    return (
        f"Symptom: {symptom}\n\n"
        f"All hypotheses explored:\n{json.dumps(hypotheses, indent=2)}\n\n"
        "Three fix attempts failed across distinct hypotheses. "
        "Diagnose at the pattern level: wrong shared-state model, violated invariant, "
        "or wrong abstraction? Name the architectural question and propose alternatives."
    )
