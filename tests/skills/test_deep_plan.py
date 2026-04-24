"""Workflow-level tests for DeepPlanWorkflow.

Activity roles faked: "planner", "architect", "critic", "adr".
All fakes return deterministic structured outputs; no real LLM calls.

Coverage:
- Happy path: consensus on iter 0 → consensus_reached_at_iter_0
- Deliberate mode: task with high-risk keyword → mode=deliberate, premortem required
- Rubber-stamp filter: all critic rejections lack executable command → APPROVE_AFTER_RUBBER_STAMP_FILTER
- Critical concern: architect returns severity=critical → Critic blocked; loops to next iter
- Max-iter no-consensus: critic always ITERATE → max_iter_no_consensus, ADR still written
- Unparseable retry → eventual terminal label after MAX_ROLE_RETRIES
"""

from __future__ import annotations

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.deep_plan.workflow import DeepPlanInput, DeepPlanWorkflow

# ---------------------------------------------------------------------------
# Role-dispatching fakes
# ---------------------------------------------------------------------------

_APPROVE_PLAN = "# Plan\n\nStep 1: Do the thing.\nStep 2: Verify.\n"
_APPROVE_CRITERIA = '["AC-001: system works"]'


@activity.defn(name="spawn_subagent")
async def _fake_spawn_consensus(inp: SpawnSubagentInput) -> dict[str, str]:
    """Immediately reach consensus on iter 0."""
    role = inp.role
    if role == "planner":
        return {"PLAN": _APPROVE_PLAN, "ACCEPTANCE_CRITERIA": _APPROVE_CRITERIA}
    if role == "architect":
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "critic":
        return {"VERDICT": "APPROVE"}
    if role == "adr":
        return {"ADR": "# ADR\n\n## Status\nAccepted\n\n## Decision\nProceed.\n"}
    return {}


@activity.defn(name="spawn_subagent")
async def _fake_spawn_max_iter(inp: SpawnSubagentInput) -> dict[str, str]:
    """Critic always returns ITERATE with a valid rejection → max_iter_no_consensus."""
    role = inp.role
    if role == "planner":
        return {"PLAN": _APPROVE_PLAN, "ACCEPTANCE_CRITERIA": _APPROVE_CRITERIA}
    if role == "architect":
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "critic":
        return {
            "VERDICT": "ITERATE",
            "REJECTION": "r1|testability|When running pytest tests/ no tests cover the auth flow|pytest tests/auth/ -v",
            "DETAILS": "Missing auth test coverage.",
        }
    if role == "adr":
        return {"ADR": "# ADR\n\n## Status\nmax_iter_no_consensus\n\n## Consensus Status\nUnresolved.\n"}
    return {}


@activity.defn(name="spawn_subagent")
async def _fake_spawn_rubber_stamp(inp: SpawnSubagentInput) -> dict[str, str]:
    """Critic rejects with vague (rubber-stamp) rejections → all dropped → promoted."""
    role = inp.role
    if role == "planner":
        return {"PLAN": _APPROVE_PLAN, "ACCEPTANCE_CRITERIA": _APPROVE_CRITERIA}
    if role == "architect":
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "critic":
        # Both rejections are vague: no executable command, vague scenario
        return {
            "VERDICT": "ITERATE",
            "REJECTION": "r1|clarity|needs more detail|review the documentation",
        }
    if role == "adr":
        return {"ADR": "# ADR\n\n## Status\nApproved (rubber-stamp filter)\n"}
    return {}


@activity.defn(name="spawn_subagent")
async def _fake_spawn_critical_concern(inp: SpawnSubagentInput) -> dict[str, str]:
    """Architect returns a critical concern on iter 0, Critic blocked; iter 1 → APPROVE."""
    role = inp.role
    # We need state across calls — use a closure via a mutable container.
    # We can't use module-level state safely in Temporal sandbox; the fake will
    # be called multiple times. Use the user_prompt_path to distinguish iterations.
    prompt_path = inp.user_prompt_path
    if role == "planner":
        return {"PLAN": _APPROVE_PLAN, "ACCEPTANCE_CRITERIA": _APPROVE_CRITERIA}
    if role == "architect":
        if "iter0" in prompt_path:
            # First iteration: critical concern
            return {
                "VERDICT": "ARCHITECT_CONCERNS",
                "CONCERN": "c1|Missing rate-limiting on the auth endpoint|critical",
                "TRADEOFF": "Speed vs. security",
            }
        # Second iteration: OK
        return {"VERDICT": "ARCHITECT_OK"}
    if role == "critic":
        return {"VERDICT": "APPROVE"}
    if role == "adr":
        return {"ADR": "# ADR\n\n## Status\nAccepted\n\n## Decision\nProceed with rate-limiting.\n"}
    return {}


@activity.defn(name="spawn_subagent")
async def _fake_spawn_unparseable(inp: SpawnSubagentInput) -> dict[str, str]:
    """Planner always returns empty dict → unparseable → terminal label after retries."""
    role = inp.role
    if role == "planner":
        return {}  # empty → unparseable
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASSTHROUGH = SandboxRestrictions.default.with_passthrough_modules(
    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
)


def _make_input(tmp_path, **kwargs) -> DeepPlanInput:
    return DeepPlanInput(
        task=kwargs.get("task", "Build a distributed rate-limiter"),
        run_id=kwargs.get("run_id", "dp-test"),
        run_dir=str(tmp_path / "run"),
        inbox_path=str(tmp_path / "INBOX.md"),
        max_iter=kwargs.get("max_iter", 3),
        deliberate=kwargs.get("deliberate", False),
        notify=False,
    )


async def _run_workflow(env, activities, inp: DeepPlanInput) -> str:
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[DeepPlanWorkflow],
        activities=[write_artifact, emit_finding, *activities],
        workflow_runner=SandboxedWorkflowRunner(restrictions=_PASSTHROUGH),
    ):
        return await env.client.execute_workflow(
            DeepPlanWorkflow.run,
            inp,
            id=inp.run_id,
            task_queue=TASK_QUEUE,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_happy_path_consensus(tmp_path) -> None:
    """Consensus reached on iteration 0 → correct label and files written."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            [_fake_spawn_consensus],
            _make_input(tmp_path, run_id="dp-happy"),
        )

    assert "consensus_reached_at_iter_0" in result

    plan_path = tmp_path / "run" / "plan.md"
    assert plan_path.exists(), "plan.md was not written"
    plan_text = plan_path.read_text()
    assert "Plan" in plan_text
    # plan.md must contain the planner's full output, not just a title stub
    assert "Step 1" in plan_text, "plan.md is missing the planner body content"
    assert "Step 2" in plan_text, "plan.md is missing the planner body content"

    adr_path = tmp_path / "run" / "adr.md"
    assert adr_path.exists(), "adr.md was not written"
    assert "ADR" in adr_path.read_text()

    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dp-happy" in inbox_text
    assert "consensus_reached" in inbox_text


async def test_deliberate_mode_auto_detected(tmp_path) -> None:
    """High-risk keyword in task → mode=deliberate reflected in summary."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            [_fake_spawn_consensus],
            _make_input(
                tmp_path,
                run_id="dp-deliberate",
                task="Migrate user auth data to new security schema",
            ),
        )

    # Summary should mention deliberate mode
    assert "mode: deliberate" in result


async def test_deliberate_mode_flag(tmp_path) -> None:
    """--deliberate flag forces deliberate mode regardless of task keywords."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            [_fake_spawn_consensus],
            _make_input(
                tmp_path,
                run_id="dp-flag",
                task="Refactor the widget module",
                deliberate=True,
            ),
        )

    assert "mode: deliberate" in result


async def test_rubber_stamp_filter_promotes_to_approved(tmp_path) -> None:
    """All critic rejections are vague → promoted to APPROVE_AFTER_RUBBER_STAMP_FILTER."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            [_fake_spawn_rubber_stamp],
            _make_input(tmp_path, run_id="dp-rubber"),
        )

    assert "rubber_stamp_filter" in result
    # Dropped-rejections file should have been written for iter 0
    dropped = list((tmp_path / "run").glob("dropped-rejections-iter*.md"))
    assert dropped, "dropped-rejections file not written"
    dropped_text = dropped[0].read_text()
    assert "rubber-stamp" in dropped_text or "vague" in dropped_text

    # plan.md must contain the planner's full body, not just a title
    plan_path = tmp_path / "run" / "plan.md"
    assert plan_path.exists(), "plan.md was not written on rubber-stamp path"
    plan_text = plan_path.read_text()
    assert "Step 1" in plan_text, "plan.md is missing planner body on rubber-stamp path"

    # ADR must still be written
    adr_path = tmp_path / "run" / "adr.md"
    assert adr_path.exists()


async def test_architect_critical_concern_blocks_critic(tmp_path) -> None:
    """Critical concern on iter 0 blocks Critic; iter 1 architect OK → consensus."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            [_fake_spawn_critical_concern],
            _make_input(tmp_path, run_id="dp-critical", max_iter=3),
        )

    # Should reach consensus on iter 1 (Critic blocked on iter 0)
    assert "consensus_reached" in result
    assert "critic_blocked_critical" in result

    adr_path = tmp_path / "run" / "adr.md"
    assert adr_path.exists()


async def test_max_iter_no_consensus_writes_adr(tmp_path) -> None:
    """max_iter exhausted → max_iter_no_consensus label AND adr.md still written."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            [_fake_spawn_max_iter],
            _make_input(tmp_path, run_id="dp-maxiter", max_iter=2),
        )

    assert "max_iter_no_consensus" in result

    # ADR must be written even without consensus
    adr_path = tmp_path / "run" / "adr.md"
    assert adr_path.exists(), "adr.md not written on max_iter_no_consensus"
    adr_text = adr_path.read_text()
    assert "ADR" in adr_text

    plan_path = tmp_path / "run" / "plan.md"
    assert plan_path.exists()

    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "NO_CONSENSUS" in inbox_text


async def test_unparseable_planner_terminates_with_label(tmp_path) -> None:
    """Planner always returns empty → after retries → terminal label containing 'unparseable'."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            [_fake_spawn_unparseable],
            _make_input(tmp_path, run_id="dp-unparse", max_iter=1),
        )

    # Terminal label must reference unparseable or spawn_failed for planner
    assert "planner" in result
    assert "unparseable" in result or "spawn_failed" in result
