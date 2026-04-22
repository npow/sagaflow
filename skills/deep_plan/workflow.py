"""deep-plan-temporal: consensus loop skill.

Phases:
  1. Planner (Haiku): generates a plan + acceptance criteria.
  2. Architect (Haiku): structural review — ARCHITECT_OK or ARCHITECT_CONCERNS.
  3. Critic (Haiku): final verdict — APPROVE | ITERATE | REJECT.
  4. ADR Scribe (Haiku): writes the Architecture Decision Record (only on consensus).

Loops up to max_iter times. Breaks when critic returns APPROVE. Otherwise feeds
architect + critic feedback back into the next planner iteration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.durable.activities import (
        EmitFindingInput,
        SpawnSubagentInput,
        WriteArtifactInput,
    )
    from sagaflow.durable.retry_policies import HAIKU_POLICY


@dataclass(frozen=True)
class DeepPlanInput:
    task: str
    run_id: str
    run_dir: str
    inbox_path: str
    max_iter: int = 5
    notify: bool = True


@workflow.defn(name="DeepPlanWorkflow")
class DeepPlanWorkflow:
    @workflow.run
    async def run(self, inp: DeepPlanInput) -> str:  # noqa: C901 (complexity ok for sequential loop)
        plan_path = f"{inp.run_dir}/plan.md"
        adr_path = f"{inp.run_dir}/adr.md"

        current_plan = ""
        current_criteria: str = "[]"
        feedback_lines: list[str] = []
        verdict_history: list[str] = []
        consensus_iter: int | None = None

        for iteration in range(inp.max_iter):
            # --- Phase 1: Planner ---
            planner_prompt_path = f"{inp.run_dir}/planner-iter{iteration}.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=planner_prompt_path,
                    content=_planner_user_prompt(
                        task=inp.task,
                        iteration=iteration,
                        previous_plan=current_plan,
                        feedback=feedback_lines,
                    ),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            planner_result = await workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="planner",
                    tier_name="HAIKU",
                    system_prompt=_planner_system_prompt(),
                    user_prompt_path=planner_prompt_path,
                    max_tokens=2048,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=HAIKU_POLICY,
            )
            current_plan = planner_result.get("PLAN", current_plan or f"# Plan\n\nTask: {inp.task}\n")
            current_criteria = planner_result.get("ACCEPTANCE_CRITERIA", "[]")

            # --- Phase 2: Architect ---
            architect_prompt_path = f"{inp.run_dir}/architect-iter{iteration}.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=architect_prompt_path,
                    content=_architect_user_prompt(
                        task=inp.task,
                        plan=current_plan,
                        criteria=current_criteria,
                    ),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            architect_result = await workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="architect",
                    tier_name="HAIKU",
                    system_prompt=_architect_system_prompt(),
                    user_prompt_path=architect_prompt_path,
                    max_tokens=1024,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=HAIKU_POLICY,
            )
            arch_verdict = architect_result.get("VERDICT", "ARCHITECT_CONCERNS")
            architect_concerns = _extract_concerns(architect_result)

            # --- Phase 3: Critic ---
            critic_prompt_path = f"{inp.run_dir}/critic-iter{iteration}.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=critic_prompt_path,
                    content=_critic_user_prompt(
                        task=inp.task,
                        plan=current_plan,
                        criteria=current_criteria,
                        architect_verdict=arch_verdict,
                        architect_concerns=architect_concerns,
                    ),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            critic_result = await workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="critic",
                    tier_name="HAIKU",
                    system_prompt=_critic_system_prompt(),
                    user_prompt_path=critic_prompt_path,
                    max_tokens=1024,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=HAIKU_POLICY,
            )
            critic_verdict = critic_result.get("VERDICT", "ITERATE")
            verdict_history.append(f"iter{iteration}:{arch_verdict}:{critic_verdict}")

            if critic_verdict == "APPROVE":
                consensus_iter = iteration
                break

            # Build feedback for next iteration.
            feedback_lines = []
            if architect_concerns:
                feedback_lines.append(f"Architect ({arch_verdict}): {'; '.join(architect_concerns)}")
            critic_details = critic_result.get("DETAILS", "")
            if critic_details:
                feedback_lines.append(f"Critic ({critic_verdict}): {critic_details}")
            elif critic_verdict in ("ITERATE", "REJECT"):
                feedback_lines.append(f"Critic verdict: {critic_verdict}. Revise the plan.")

        # Save plan.md regardless of outcome.
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=plan_path, content=current_plan),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        # --- Phase 4: ADR Scribe (only on consensus) ---
        if consensus_iter is not None:
            adr_prompt_path = f"{inp.run_dir}/adr-prompt.txt"
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(
                    path=adr_prompt_path,
                    content=_adr_user_prompt(task=inp.task, plan=current_plan, criteria=current_criteria),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            adr_result = await workflow.execute_activity(
                "spawn_subagent",
                SpawnSubagentInput(
                    role="adr",
                    tier_name="HAIKU",
                    system_prompt=_adr_system_prompt(),
                    user_prompt_path=adr_prompt_path,
                    max_tokens=2048,
                    tools_needed=False,
                ),
                start_to_close_timeout=timedelta(seconds=180),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=HAIKU_POLICY,
            )
            adr_md = adr_result.get("ADR", _fallback_adr(inp.task, current_plan))
            await workflow.execute_activity(
                "write_artifact",
                WriteArtifactInput(path=adr_path, content=adr_md),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            terminal_label = f"consensus_reached_at_iter_{consensus_iter}"
        else:
            terminal_label = "max_iter_no_consensus"

        summary = (
            f"{terminal_label} after {len(verdict_history)} iteration(s); "
            f"verdicts: {', '.join(verdict_history)}"
        )
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="deep-plan",
                status="DONE" if consensus_iter is not None else "NO_CONSENSUS",
                summary=summary,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return f"{summary}\nPlan: {plan_path}"


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def _planner_system_prompt() -> str:
    return (
        "You are a technical planner. Given a task description (and optionally feedback "
        "from a previous iteration), write a clear, actionable implementation plan in "
        "Markdown plus a JSON array of acceptance criteria.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "PLAN|<full markdown plan — use literal newlines inside the value>\n"
        "ACCEPTANCE_CRITERIA|<json array of strings — each entry is one criterion>\n"
        "STRUCTURED_OUTPUT_END\n"
        "IMPORTANT: PLAN value starts immediately after the first pipe. Do not add "
        "extra pipe characters inside the plan body."
    )


def _planner_user_prompt(
    *,
    task: str,
    iteration: int,
    previous_plan: str,
    feedback: list[str],
) -> str:
    parts = [f"Task: {task}", f"Iteration: {iteration}"]
    if previous_plan:
        parts.append(f"\n--- PREVIOUS PLAN ---\n{previous_plan}\n--- END PREVIOUS PLAN ---")
    if feedback:
        parts.append("\n--- FEEDBACK TO ADDRESS ---")
        parts.extend(f"  - {f}" for f in feedback)
        parts.append("--- END FEEDBACK ---")
    parts.append("\nWrite or revise the plan addressing the feedback above.")
    return "\n".join(parts)


def _architect_system_prompt() -> str:
    return (
        "You are a senior software architect. Review the proposed plan for structural "
        "soundness: correctness, scalability, testability, dependency risks, and "
        "completeness relative to the acceptance criteria.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "VERDICT|ARCHITECT_OK\n"
        "STRUCTURED_OUTPUT_END\n"
        "-- OR --\n"
        "STRUCTURED_OUTPUT_START\n"
        "VERDICT|ARCHITECT_CONCERNS\n"
        "CONCERN_1|<first concern>\n"
        "CONCERN_2|<second concern>\n"
        "STRUCTURED_OUTPUT_END\n"
        "Emit ARCHITECT_OK only when the plan has no structural issues. "
        "List each concern on its own CONCERN_N line."
    )


def _architect_user_prompt(*, task: str, plan: str, criteria: str) -> str:
    return (
        f"Task: {task}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "--- PLAN START ---\n"
        f"{plan}\n"
        "--- PLAN END ---\n"
    )


def _critic_system_prompt() -> str:
    return (
        "You are a rigorous plan critic. Given the plan, acceptance criteria, and the "
        "architect's review, decide whether to APPROVE, ITERATE, or REJECT.\n\n"
        "APPROVE   — plan is solid; proceed.\n"
        "ITERATE   — plan has fixable issues; provide concise guidance.\n"
        "REJECT    — plan is fundamentally flawed; explain why.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "VERDICT|APPROVE\n"
        "STRUCTURED_OUTPUT_END\n"
        "-- OR --\n"
        "STRUCTURED_OUTPUT_START\n"
        "VERDICT|ITERATE\n"
        "DETAILS|<one-paragraph critique>\n"
        "STRUCTURED_OUTPUT_END\n"
        "Be decisive. Don't ITERATE more than necessary."
    )


def _critic_user_prompt(
    *,
    task: str,
    plan: str,
    criteria: str,
    architect_verdict: str,
    architect_concerns: list[str],
) -> str:
    concerns_text = "\n".join(f"  - {c}" for c in architect_concerns) if architect_concerns else "  (none)"
    return (
        f"Task: {task}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"Architect verdict: {architect_verdict}\n"
        f"Architect concerns:\n{concerns_text}\n\n"
        "--- PLAN START ---\n"
        f"{plan}\n"
        "--- PLAN END ---\n"
    )


def _adr_system_prompt() -> str:
    return (
        "You are a technical writer specializing in Architecture Decision Records (ADRs). "
        "Write a concise ADR for the approved plan.\n\n"
        "Output format:\n"
        "STRUCTURED_OUTPUT_START\n"
        "ADR|<full ADR markdown — use literal newlines inside the value>\n"
        "STRUCTURED_OUTPUT_END\n"
        "Structure: Title, Status, Context, Decision, Consequences. "
        "Keep it concise and precise."
    )


def _adr_user_prompt(*, task: str, plan: str, criteria: str) -> str:
    return (
        f"Task: {task}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "--- APPROVED PLAN START ---\n"
        f"{plan}\n"
        "--- APPROVED PLAN END ---\n\n"
        "Write the ADR."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_concerns(architect_result: dict[str, str]) -> list[str]:
    """Collect CONCERN_1, CONCERN_2, … values from the structured output dict."""
    concerns: list[str] = []
    i = 1
    while True:
        key = f"CONCERN_{i}"
        val = architect_result.get(key)
        if val is None:
            break
        concerns.append(val)
        i += 1
    return concerns


def _fallback_adr(task: str, plan: str) -> str:
    return (
        "# ADR (fallback)\n\n"
        f"## Status\nAccepted\n\n"
        f"## Context\n{task}\n\n"
        "## Decision\nSee the approved plan.\n\n"
        "## Consequences\nPlan output follows:\n\n"
        f"{plan[:2000]}\n"
    )
