"""autopilot-temporal: full-lifecycle orchestrator.

MVP port: instead of child-workflow delegation to deep-plan/team/deep-qa/
loop-until-done, inline a simplified version of each phase as a direct
spawn_subagent call. The phase-gate pattern (iron-law advance) is preserved
via structured-output verdict parsing.

Phases:
  1. expand — ambiguity classifier (Haiku) + spec-draft (Sonnet)
  2. plan — simplified planner (Sonnet): plan + acceptance criteria
  3. exec — 2 parallel workers (Sonnet) implement
  4. qa — QA auditor (Sonnet) returns AUDIT_LABEL
  5. validate — 3 judges in parallel (Opus) return APPROVED/REJECTED
  6. cleanup — completion report

Deferred: true child-workflow delegation, fix loop, budget enforcement.
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
class AutopilotInput:
    run_id: str
    initial_idea: str
    inbox_path: str
    run_dir: str
    notify: bool = True


@workflow.defn(name="AutopilotWorkflow")
class AutopilotWorkflow:
    @workflow.run
    async def run(self, inp: AutopilotInput) -> str:
        report_path = f"{inp.run_dir}/completion-report.md"
        phases_passed: list[str] = []

        # Phase 1: expand
        expand = await _spawn(
            inp.run_dir, "expand", "SONNET",
            "Classify ambiguity and draft a spec. Emit\n"
            "STRUCTURED_OUTPUT_START\n"
            "AMBIGUITY_CLASS|low|medium|high\n"
            "SPEC|<spec markdown>\n"
            "STRUCTURED_OUTPUT_END",
            f"Idea: {inp.initial_idea}",
            max_tokens=1024,
        )
        if expand.get("SPEC"):
            phases_passed.append("expand")
        spec = expand.get("SPEC", inp.initial_idea)

        # Phase 2: plan
        plan = await _spawn(
            inp.run_dir, "plan", "SONNET",
            "Produce a plan with acceptance criteria. Emit\n"
            "STRUCTURED_OUTPUT_START\n"
            "PLAN|<markdown>\n"
            "ACCEPTANCE_CRITERIA|<json array of strings>\n"
            "STRUCTURED_OUTPUT_END",
            f"Spec: {spec}",
            max_tokens=1024,
        )
        if plan.get("PLAN"):
            phases_passed.append("plan")

        # Phase 3: exec (2 parallel workers)
        exec_results = await asyncio.gather(*[
            _spawn(
                inp.run_dir, "worker", "SONNET",
                "Implement your slice. Emit\n"
                "STRUCTURED_OUTPUT_START\n"
                "WORK_SUMMARY|<what you did>\n"
                "STRUCTURED_OUTPUT_END",
                f"Plan: {plan.get('PLAN', '')}\nWorker index: {i}",
                max_tokens=1024,
                role_suffix=f"-{i}",
            )
            for i in range(2)
        ], return_exceptions=True)
        if any(not isinstance(r, BaseException) for r in exec_results):
            phases_passed.append("exec")

        # Phase 4: qa
        qa = await _spawn(
            inp.run_dir, "qa", "SONNET",
            "Audit the work. Emit\n"
            "STRUCTURED_OUTPUT_START\n"
            "AUDIT_LABEL|clean|defects_found\n"
            "CRITICAL_COUNT|<int>\n"
            "STRUCTURED_OUTPUT_END",
            f"Plan: {plan.get('PLAN', '')}\n"
            f"Worker outputs: {json.dumps([str(r) for r in exec_results])}",
            max_tokens=1024,
        )
        if qa.get("AUDIT_LABEL"):
            phases_passed.append("qa")

        # Phase 5: validate — 3 parallel judges
        judge_results = await asyncio.gather(*[
            _spawn(
                inp.run_dir, f"judge-{dim}", "OPUS",
                f"Judge the {dim} dimension. Emit\n"
                "STRUCTURED_OUTPUT_START\n"
                "VERDICT|approved|rejected|conditional\n"
                "STRUCTURED_OUTPUT_END",
                f"Plan: {plan.get('PLAN', '')}\nAudit: {qa.get('AUDIT_LABEL', '')}",
                max_tokens=512,
            )
            for dim in ["correctness", "security", "quality"]
        ], return_exceptions=True)
        judge_verdicts: list[str] = []
        for r in judge_results:
            if isinstance(r, BaseException):
                judge_verdicts.append("rejected")
            else:
                judge_verdicts.append(r.get("VERDICT", "rejected"))
        approved_count = sum(1 for v in judge_verdicts if v == "approved")
        if approved_count >= 2:
            phases_passed.append("validate")
            aggregate = "approved" if approved_count == 3 else "conditional"
        else:
            aggregate = "rejected"

        # Phase 6: cleanup — write report + emit finding
        if aggregate == "approved" and len(phases_passed) == 5:
            termination = "complete"
        elif aggregate == "conditional":
            termination = "partial_with_accepted_tradeoffs"
        else:
            termination = f"blocked_at_phase_{len(phases_passed) + 1}"

        report_md = (
            f"# Autopilot Completion Report\n\n"
            f"**Idea:** {inp.initial_idea}\n\n"
            f"**Termination:** {termination}\n\n"
            f"**Phases passed:** {', '.join(phases_passed) or 'none'}\n\n"
            f"**Judge verdicts:** {judge_verdicts}\n\n"
            f"**Audit:** {qa.get('AUDIT_LABEL', 'unknown')}\n"
        )
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
                skill="autopilot",
                status="DONE",
                summary=f"{termination}: {len(phases_passed)}/5 phases passed",
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{termination}\nReport: {report_path}"


async def _spawn(
    run_dir: str,
    role: str,
    tier: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    role_suffix: str = "",
) -> dict[str, str]:
    prompt_path = f"{run_dir}/{role}{role_suffix}-prompt.txt"
    await workflow.execute_activity(
        "write_artifact",
        WriteArtifactInput(path=prompt_path, content=user_prompt),
        start_to_close_timeout=timedelta(seconds=10),
        retry_policy=HAIKU_POLICY,
    )
    policy = SONNET_POLICY if tier in ("SONNET", "OPUS") else HAIKU_POLICY
    result: dict[str, str] = await workflow.execute_activity(
        "spawn_subagent",
        SpawnSubagentInput(
            role=role,
            tier_name=tier,
            system_prompt=system_prompt,
            user_prompt_path=prompt_path,
            max_tokens=max_tokens,
            tools_needed=False,
        ),
        start_to_close_timeout=timedelta(seconds=240),
        heartbeat_timeout=timedelta(seconds=60),
        retry_policy=policy,
    )
    return result
