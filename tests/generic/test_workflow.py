"""Workflow-level tests for the generic claude-skills interpreter.

Uses Temporal's time-skipping environment with a fake ``call_claude_with_tools``
activity that returns a scripted sequence of responses. The scripted-response
pattern lets us drive the workflow deterministically through every tool-use
branch (end_turn, parallel dispatch, max_iterations, child-workflow spawn, and
the subagent allow-list denial path).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from sagaflow.durable.activities import (
    emit_finding,
    write_artifact,
)
from sagaflow.generic.activities import (
    CallClaudeInput,
    ClaudeResponse,
    ClaudeToolUse,
    generic_tool_adapter_bash_tool,
    generic_tool_adapter_glob_tool,
    generic_tool_adapter_grep_tool,
    generic_tool_adapter_read_file_tool,
    generic_tool_adapter_write_artifact,
)
from sagaflow.generic.workflow import (
    ClaudeSkillInput,
    ClaudeSkillWorkflow,
    SubagentInput,
    SubagentWorkflow,
)
from sagaflow.temporal_client import TASK_QUEUE


# ---------------------------------------------------------------------------
# Scripted-response fixture: builds a fake `call_claude_with_tools` activity
# that returns responses from a shared list in invocation order. The fixture
# ALSO supports a callable for cases where the response depends on the input
# messages (e.g. subagent tests where the parent's invocation and the child's
# invocation need different responses).
# ---------------------------------------------------------------------------


class _ClaudeScript:
    """Holds a list of scripted responses + a call log. Acts as a factory for the fake activity."""

    def __init__(
        self,
        responses: list[ClaudeResponse] | Callable[[object], ClaudeResponse],
    ):
        self._responses = responses
        self.calls: list[dict] = []  # logged CallClaudeInput.messages per invocation

    def build(self):
        responses = self._responses
        calls = self.calls

        @activity.defn(name="call_claude_with_tools")
        async def _fake_call_claude_with_tools(inp: CallClaudeInput) -> ClaudeResponse:
            calls.append(
                {
                    "messages": list(inp.messages),
                    "tools": list(inp.tools),
                    "system_prompt": inp.system_prompt,
                    "tier_name": inp.tier_name,
                }
            )
            if callable(responses):
                return responses(inp)
            if not responses:
                # No script left — default to a benign end_turn.
                return ClaudeResponse(text="(empty)", tool_uses=[], stop_reason="end_turn")
            return responses.pop(0)

        return _fake_call_claude_with_tools


def _tool_use(name: str, inp: dict, tu_id: str | None = None) -> ClaudeToolUse:
    return ClaudeToolUse(
        id=tu_id or f"toolu_{name}_{abs(hash((name, str(inp)))) % 10_000_000}",
        name=name,
        input=inp,
    )


def _make_input(tmp_path: Path, **overrides) -> ClaudeSkillInput:
    defaults = dict(
        run_id="gen-1",
        run_dir=str(tmp_path / "runs" / "gen-1"),
        inbox_path=str(tmp_path / "INBOX.md"),
        skill_name="test-skill",
        skill_md_content="# Test skill\n\nDo the thing.",
        user_args={"name": "alice"},
        max_iterations=10,
        tier_name="SONNET",
        notify=False,
    )
    defaults.update(overrides)
    return ClaudeSkillInput(**defaults)


async def _run_workflow(
    env: WorkflowEnvironment,
    *,
    workflows: list,
    activities: list,
    workflow_input: ClaudeSkillInput,
    wf_id: str = "gen-1",
) -> str:
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=workflows,
        activities=activities,
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "httpx", "anthropic", "sagaflow"
            )
        ),
    ):
        return await env.client.execute_workflow(
            ClaudeSkillWorkflow.run,
            workflow_input,
            id=wf_id,
            task_queue=TASK_QUEUE,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_workflow_completes_on_end_turn(tmp_path) -> None:
    """Claude returns end_turn immediately; workflow writes report and emits finding."""

    script = _ClaudeScript(
        [
            ClaudeResponse(
                text="done",
                tool_uses=[],
                stop_reason="end_turn",
            )
        ]
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
            activities=[
                script.build(),
                write_artifact,
                emit_finding,
                generic_tool_adapter_read_file_tool,
                generic_tool_adapter_bash_tool,
                generic_tool_adapter_grep_tool,
                generic_tool_adapter_glob_tool,
                generic_tool_adapter_write_artifact,
            ],
            workflow_input=_make_input(tmp_path),
        )

    assert result == "done"
    # Workflow exited after a single Claude turn.
    assert len(script.calls) == 1
    # Report + INBOX written.
    report_path = tmp_path / "runs" / "gen-1" / "run-summary.md"
    inbox_path = tmp_path / "INBOX.md"
    assert report_path.exists(), "workflow must write run-summary.md to run_dir"
    assert inbox_path.exists(), "workflow must emit finding to INBOX"
    body = report_path.read_text(encoding="utf-8")
    assert "done" in body


async def test_workflow_dispatches_write_artifact_tool_use(tmp_path) -> None:
    """Claude asks for write_artifact; workflow dispatches it; next turn end_turn."""

    target_path = tmp_path / "runs" / "gen-1" / "plan.md"
    script = _ClaudeScript(
        [
            # Turn 1: Claude calls write_artifact with a plan.
            ClaudeResponse(
                text="writing the plan",
                tool_uses=[
                    _tool_use(
                        "write_artifact",
                        {
                            "path": str(target_path),
                            "content": "# plan\n\nstep one",
                        },
                        tu_id="tu-wa-1",
                    )
                ],
                stop_reason="tool_use",
            ),
            # Turn 2: Claude acknowledges + ends.
            ClaudeResponse(
                text="all done",
                tool_uses=[],
                stop_reason="end_turn",
            ),
        ]
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
            activities=[
                script.build(),
                write_artifact,
                emit_finding,
                generic_tool_adapter_read_file_tool,
                generic_tool_adapter_bash_tool,
                generic_tool_adapter_grep_tool,
                generic_tool_adapter_glob_tool,
                generic_tool_adapter_write_artifact,
            ],
            workflow_input=_make_input(tmp_path),
        )

    assert result == "all done"
    assert target_path.exists(), "workflow must execute the write_artifact tool"
    assert target_path.read_text(encoding="utf-8") == "# plan\n\nstep one"

    # Turn 2's messages must include a tool_result block referencing tu-wa-1.
    turn2 = script.calls[1]["messages"]
    assistant_msg = turn2[-2]
    tool_result_msg = turn2[-1]
    assert assistant_msg["role"] == "assistant"
    assert any(b.get("id") == "tu-wa-1" for b in assistant_msg["content"] if isinstance(b, dict))
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert tool_result_msg["content"][0]["tool_use_id"] == "tu-wa-1"


async def test_workflow_parallel_tool_use_dispatch(tmp_path) -> None:
    """Three tool_uses in one Claude turn all run; all three results come back."""

    # Each write_artifact targets a different file so we can verify all three ran.
    run_dir = tmp_path / "runs" / "gen-1"
    path_a = run_dir / "a.txt"
    path_b = run_dir / "b.txt"
    path_c = run_dir / "c.txt"

    script = _ClaudeScript(
        [
            ClaudeResponse(
                text="writing three files in parallel",
                tool_uses=[
                    _tool_use(
                        "write_artifact",
                        {"path": str(path_a), "content": "A"},
                        tu_id="tu-a",
                    ),
                    _tool_use(
                        "write_artifact",
                        {"path": str(path_b), "content": "B"},
                        tu_id="tu-b",
                    ),
                    _tool_use(
                        "write_artifact",
                        {"path": str(path_c), "content": "C"},
                        tu_id="tu-c",
                    ),
                ],
                stop_reason="tool_use",
            ),
            ClaudeResponse(text="all three wrote", tool_uses=[], stop_reason="end_turn"),
        ]
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run_workflow(
            env,
            workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
            activities=[
                script.build(),
                write_artifact,
                emit_finding,
                generic_tool_adapter_read_file_tool,
                generic_tool_adapter_bash_tool,
                generic_tool_adapter_grep_tool,
                generic_tool_adapter_glob_tool,
                generic_tool_adapter_write_artifact,
            ],
            workflow_input=_make_input(tmp_path),
        )

    assert path_a.read_text(encoding="utf-8") == "A"
    assert path_b.read_text(encoding="utf-8") == "B"
    assert path_c.read_text(encoding="utf-8") == "C"

    # Turn 2 must have exactly 3 tool_result blocks, one per tool_use_id.
    turn2 = script.calls[1]["messages"]
    tool_result_msg = turn2[-1]
    assert len(tool_result_msg["content"]) == 3
    tr_ids = {b["tool_use_id"] for b in tool_result_msg["content"]}
    assert tr_ids == {"tu-a", "tu-b", "tu-c"}


async def test_workflow_max_iterations_cap(tmp_path) -> None:
    """Fake Claude always asks for another write_artifact — workflow stops at max_iterations."""

    run_dir = tmp_path / "runs" / "gen-1"
    target = run_dir / "loop.txt"

    # Script is a callable so it never runs out — infinite loop.
    def _always_tool_use(inp):  # type: ignore[no-untyped-def]
        return ClaudeResponse(
            text="still looping",
            tool_uses=[
                _tool_use(
                    "write_artifact",
                    {"path": str(target), "content": "x"},
                    tu_id=f"tu-loop-{len(script.calls)}",
                )
            ],
            stop_reason="tool_use",
        )

    script = _ClaudeScript(_always_tool_use)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        await _run_workflow(
            env,
            workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
            activities=[
                script.build(),
                write_artifact,
                emit_finding,
                generic_tool_adapter_read_file_tool,
                generic_tool_adapter_bash_tool,
                generic_tool_adapter_grep_tool,
                generic_tool_adapter_glob_tool,
                generic_tool_adapter_write_artifact,
            ],
            workflow_input=_make_input(tmp_path, max_iterations=3),
        )

    # Exactly max_iterations calls into Claude, no more.
    assert len(script.calls) == 3
    # Report must note truncation.
    report = (run_dir / "run-summary.md").read_text(encoding="utf-8")
    assert "max_iterations" in report or "NOTE" in report
    # INBOX finding should be TRUNCATED status.
    inbox = (tmp_path / "INBOX.md").read_text(encoding="utf-8")
    assert "TRUNCATED" in inbox


async def test_subagent_child_workflow_round_trip(tmp_path) -> None:
    """Parent dispatches SubagentWorkflow via spawn_subagent; child returns text to parent."""

    run_dir = tmp_path / "runs" / "gen-1"

    # Two scripted response lists: one for each workflow_id. We key off the
    # system prompt (parent's contains "SKILL.md", child's contains "subagent")
    # to tell them apart.
    def _script(inp):  # type: ignore[no-untyped-def]
        is_parent = "SKILL.md" in inp.system_prompt
        if is_parent:
            if parent_calls_ref[0] == 0:
                parent_calls_ref[0] += 1
                return ClaudeResponse(
                    text="delegating to researcher",
                    tool_uses=[
                        _tool_use(
                            "spawn_subagent",
                            {
                                "role": "researcher",
                                "system_prompt": "You are a researcher.",
                                "user_prompt": "Find the capital of France.",
                                "tools": [],
                                "tier_name": "HAIKU",
                                "max_iterations": 5,
                            },
                            tu_id="tu-spawn-1",
                        )
                    ],
                    stop_reason="tool_use",
                )
            parent_calls_ref[0] += 1
            return ClaudeResponse(
                text=f"researcher said: {last_child_reply_ref[0]}",
                tool_uses=[],
                stop_reason="end_turn",
            )
        # Child (subagent) context.
        child_reply = "paris"
        last_child_reply_ref[0] = child_reply
        return ClaudeResponse(text=child_reply, tool_uses=[], stop_reason="end_turn")

    parent_calls_ref = [0]
    last_child_reply_ref = [""]
    script = _ClaudeScript(_script)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
            activities=[
                script.build(),
                write_artifact,
                emit_finding,
                generic_tool_adapter_read_file_tool,
                generic_tool_adapter_bash_tool,
                generic_tool_adapter_grep_tool,
                generic_tool_adapter_glob_tool,
                generic_tool_adapter_write_artifact,
            ],
            workflow_input=_make_input(tmp_path),
        )

    assert result.startswith("researcher said:")
    assert "paris" in result

    # The parent's second Claude call should have a tool_result containing the
    # child's reply text.
    parent_turn2_idx = None
    for i, call in enumerate(script.calls):
        if "SKILL.md" in call["system_prompt"] and i > 0:
            parent_turn2_idx = i
            break
    assert parent_turn2_idx is not None, "parent never got the child's reply"
    parent_turn2 = script.calls[parent_turn2_idx]["messages"]
    tool_result_msg = parent_turn2[-1]
    assert tool_result_msg["content"][0]["tool_use_id"] == "tu-spawn-1"
    assert "paris" in tool_result_msg["content"][0]["content"]


async def test_subagent_denied_tool_returns_error_to_claude(tmp_path) -> None:
    """Subagent's allow-list excludes 'bash'; Claude tries bash; workflow returns
    an error tool_result so Claude can reroute."""

    def _script(inp):  # type: ignore[no-untyped-def]
        is_parent = "SKILL.md" in inp.system_prompt
        if is_parent:
            if parent_calls[0] == 0:
                parent_calls[0] += 1
                return ClaudeResponse(
                    text="delegating",
                    tool_uses=[
                        _tool_use(
                            "spawn_subagent",
                            {
                                "role": "restricted",
                                "system_prompt": "You are restricted.",
                                "user_prompt": "Do research.",
                                # No tools allowed at all.
                                "tools": [],
                                "tier_name": "HAIKU",
                                "max_iterations": 5,
                            },
                            tu_id="tu-spawn-deny",
                        )
                    ],
                    stop_reason="tool_use",
                )
            parent_calls[0] += 1
            return ClaudeResponse(
                text=f"subagent returned: {last_child_reply[0]}",
                tool_uses=[],
                stop_reason="end_turn",
            )
        # Child. Turn 1 tries bash; turn 2 reacts to the denial and ends.
        if child_calls[0] == 0:
            child_calls[0] += 1
            return ClaudeResponse(
                text="going to run bash",
                tool_uses=[
                    _tool_use(
                        "bash",
                        {"command": "echo nope"},
                        tu_id="tu-bash-deny",
                    )
                ],
                stop_reason="tool_use",
            )
        child_calls[0] += 1
        # Extract the denial text from the messages (proves Claude sees it).
        last_tool_result = _find_last_tool_result(inp.messages)
        last_child_reply[0] = last_tool_result or ""
        return ClaudeResponse(
            text=f"denied: {last_tool_result}",
            tool_uses=[],
            stop_reason="end_turn",
        )

    parent_calls = [0]
    child_calls = [0]
    last_child_reply = [""]
    script = _ClaudeScript(_script)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        result = await _run_workflow(
            env,
            workflows=[ClaudeSkillWorkflow, SubagentWorkflow],
            activities=[
                script.build(),
                write_artifact,
                emit_finding,
                generic_tool_adapter_read_file_tool,
                generic_tool_adapter_bash_tool,
                generic_tool_adapter_grep_tool,
                generic_tool_adapter_glob_tool,
                generic_tool_adapter_write_artifact,
            ],
            workflow_input=_make_input(tmp_path),
        )

    # The parent's final text should contain the denial reply.
    assert "denied" in result or "not allowed" in result
    # The denial string must have actually gone back to the child.
    assert "not allowed" in last_child_reply[0]


def _find_last_tool_result(messages) -> str | None:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return block.get("content")
    return None
