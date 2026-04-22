"""team-temporal: sequential 5-stage pipeline with iron-law gates.

MVP port of the team skill:
  1. plan (Opus) — decompose task into subtasks
  2. prd (Opus) — write PRD with falsifiable acceptance criteria
  3. exec — N parallel workers implement their subtasks (Sonnet)
  4. verify (Opus) — spec-compliance + code-quality review
  5. fix (conditional, up to 3 iterations) — fix-worker + re-verify

Each stage gates on the next via structured-output verdict. Deferred: iron-law
gate evidence checks, deep-qa --diff integration, watchdog/heartbeat nuances.
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
class TeamInput:
    run_id: str
    task: str
    inbox_path: str
    run_dir: str
    n_workers: int = 2
    max_fix_iters: int = 3
    notify: bool = True


@workflow.defn(name="TeamWorkflow")
class TeamWorkflow:
    @workflow.run
    async def run(self, inp: TeamInput) -> str:
        summary_path = f"{inp.run_dir}/SUMMARY.md"

        # Stage 1: plan
        plan = await _spawn(
            inp.run_dir, "planner", "SONNET",
            "Decompose the task into subtasks. Emit "
            "STRUCTURED_OUTPUT_START\n"
            'SUBTASKS|<json array of {id, title, description}>\n'
            "STRUCTURED_OUTPUT_END",
            f"Task: {inp.task}\n\nDecompose into {inp.n_workers} subtasks.",
            max_tokens=1024,
        )
        subtasks = _parse_json_list(plan.get("SUBTASKS", "[]"))[:inp.n_workers]
        if not subtasks:
            # Fallback if planner returns nothing: single task block.
            subtasks = [{"id": "t1", "title": inp.task, "description": inp.task}]

        # Stage 2: PRD with acceptance criteria
        prd = await _spawn(
            inp.run_dir, "analyst", "SONNET",
            "Write a PRD. Emit STRUCTURED_OUTPUT_START\n"
            'ACCEPTANCE_CRITERIA|<json array of {id, criterion, verification_hint}>\n'
            "STRUCTURED_OUTPUT_END",
            f"Task: {inp.task}\nSubtasks: {json.dumps(subtasks)}",
            max_tokens=1024,
        )
        acceptance = _parse_json_list(prd.get("ACCEPTANCE_CRITERIA", "[]"))

        # Stage 3: parallel workers
        worker_results = await asyncio.gather(*[
            _spawn(
                inp.run_dir, "worker", "SONNET",
                "You are a worker. Implement the subtask. Emit "
                "STRUCTURED_OUTPUT_START\n"
                "WORK_SUMMARY|<what you did>\n"
                "FILES_TOUCHED|<json array of paths>\n"
                "STRUCTURED_OUTPUT_END",
                f"Subtask: {json.dumps(t)}\n\nPRD criteria: {json.dumps(acceptance)}",
                max_tokens=1024,
            )
            for t in subtasks
        ], return_exceptions=True)

        worker_outputs: list[dict[str, str]] = []
        for t, r in zip(subtasks, worker_results):
            if isinstance(r, BaseException):
                worker_outputs.append({"id": t.get("id", "?"), "status": "failed"})
                continue
            worker_outputs.append({
                "id": t.get("id", "?"),
                "work_summary": r.get("WORK_SUMMARY", ""),
                "files_touched": r.get("FILES_TOUCHED", "[]"),
                "status": "complete",
            })

        # Stage 4: verify (single pass)
        verify = await _spawn(
            inp.run_dir, "verifier", "SONNET",
            "Review the work against the PRD. Emit "
            "STRUCTURED_OUTPUT_START\n"
            "VERDICT|passed|failed_fixable|failed_unfixable\n"
            "DEFECTS|<json array of {id, severity, description}>\n"
            "STRUCTURED_OUTPUT_END",
            f"PRD: {json.dumps(acceptance)}\n\nWork: {json.dumps(worker_outputs)}",
            max_tokens=1024,
        )
        verdict = verify.get("VERDICT", "failed_fixable")
        defects = _parse_json_list(verify.get("DEFECTS", "[]"))

        # Stage 5: fix loop (conditional, up to max_fix_iters)
        fix_iters = 0
        while verdict == "failed_fixable" and fix_iters < inp.max_fix_iters and defects:
            fix_iters += 1
            fix = await _spawn(
                inp.run_dir, "fix-worker", "SONNET",
                "Fix the defects. Emit "
                "STRUCTURED_OUTPUT_START\n"
                "FIX_SUMMARY|<what changed>\n"
                "STRUCTURED_OUTPUT_END",
                f"Defects: {json.dumps(defects)}\n\nPRD: {json.dumps(acceptance)}",
                max_tokens=1024,
                role_suffix=f"-iter{fix_iters}",
            )
            # Re-verify
            verify = await _spawn(
                inp.run_dir, "verifier", "SONNET",
                "Re-verify after fix. Emit "
                "STRUCTURED_OUTPUT_START\n"
                "VERDICT|passed|failed_fixable|failed_unfixable\n"
                "DEFECTS|<json array>\n"
                "STRUCTURED_OUTPUT_END",
                f"PRD: {json.dumps(acceptance)}\n"
                f"Prior defects: {json.dumps(defects)}\n"
                f"Fix: {fix.get('FIX_SUMMARY', '')}",
                max_tokens=1024,
                role_suffix=f"-iter{fix_iters}",
            )
            verdict = verify.get("VERDICT", "failed_fixable")
            defects = _parse_json_list(verify.get("DEFECTS", "[]"))

        if verdict == "passed":
            label = "complete"
        elif fix_iters >= inp.max_fix_iters:
            label = "budget_exhausted"
        else:
            label = "blocked_unresolved"

        summary_md = (
            f"# Team Run Summary\n\n"
            f"**Task:** {inp.task}\n\n"
            f"**Termination:** {label}\n\n"
            f"**Workers:** {len(worker_outputs)}\n\n"
            f"**Fix iterations:** {fix_iters}\n\n"
            f"**Open defects:** {len(defects)}\n"
        )
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=summary_path, content=summary_md),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="team",
                status="DONE",
                summary=f"{label}: {len(worker_outputs)} workers, {fix_iters} fix iters",
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{label}\nSummary: {summary_path}"


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
    policy = SONNET_POLICY if tier == "SONNET" else HAIKU_POLICY
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


def _parse_json_list(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass
    return []
