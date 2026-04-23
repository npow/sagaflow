"""Generic claude-skills interpreter workflow.

``ClaudeSkillWorkflow`` drives any claude-skill's ``SKILL.md`` durably on Temporal
by running Claude's tool-use loop as a sequence of Temporal activities (or
child workflows for ``spawn_subagent``). Every step Claude takes — each tool
call and each LLM turn — is an independent Temporal event, so a worker crash
mid-run resumes from the last completed step.

Two workflows live here:

* ``ClaudeSkillWorkflow`` — top-level coordinator. Runs Claude against the
  full tool palette (``ALL_TOOLS``), emits a final report + INBOX finding.
* ``SubagentWorkflow`` — child workflow spawned when Claude invokes the
  ``spawn_subagent`` tool. Same tool-use loop but scoped to an allow-listed
  subset of tools; returns its final text back to the parent as a
  ``tool_result`` string.

Non-obvious Temporal bits (documented inline where they arise):

* Tool dispatch fan-out uses ``asyncio.gather`` so multiple tool calls in one
  Claude turn run in parallel activities.
* ``spawn_subagent`` is dispatched via ``workflow.start_child_workflow`` (not
  ``execute_activity``) so the subagent's own tool-use steps are each durable
  in its own child event history.
* We pass tool args as an ``input_dict`` (plain dict) into adapter activities,
  which marshal the dict into the typed dataclass expected by the underlying
  tool-handler activity. This keeps the dispatch table in the workflow simple
  and avoids temporal-sandbox imports of the tool dataclasses.
* Tool results go back to Claude in Anthropic's expected shape:
  ``{"type": "tool_result", "tool_use_id": ..., "content": "..."}``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.durable.activities import (
        EmitFindingInput,
        WriteArtifactInput,
    )
    from sagaflow.durable.retry_policies import HAIKU_POLICY, SONNET_POLICY
    from sagaflow.generic.activities import (
        CallClaudeInput,
        ClaudeResponse,
        ClaudeToolUse,
    )
    from sagaflow.generic.tools import ALL_TOOLS, TOOL_HANDLERS
    from sagaflow.temporal_client import TASK_QUEUE


# Conversation history cap: once total messages exceeds this, older tool_result
# pairs are pruned by ``_trim_history``. Keep both the triggering assistant
# turn and its matching user-tool-results reply, or Claude refuses to continue.
_HISTORY_MAX_MESSAGES = 100
_HISTORY_KEEP_TAIL = 60

# Timeouts per tool-handler activity. Bash runs arbitrary shell so it gets the
# longest leash; read/grep/glob are expected to return within seconds.
_TOOL_ACTIVITY_TIMEOUTS: dict[str, tuple[int, int]] = {
    # name -> (start_to_close_seconds, heartbeat_seconds)
    "write_artifact": (10, 0),
    "read_file_tool": (15, 30),
    "bash_tool": (120, 60),
    "grep_tool": (30, 30),
    "glob_tool": (30, 30),
}

# Max tokens per Claude call. Generous so tool-use responses don't truncate.
_CLAUDE_MAX_TOKENS = 4096

# Tools Claude is permitted to use in each context.
_COORDINATOR_TOOL_NAMES: list[str] = [t["name"] for t in ALL_TOOLS]


# ---------------------------------------------------------------------------
# Workflow input dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaudeSkillInput:
    run_id: str
    run_dir: str
    inbox_path: str
    skill_name: str
    skill_md_content: str
    user_args: dict[str, str] = field(default_factory=dict)
    max_iterations: int = 50
    tier_name: str = "SONNET"  # top-level coordinator tier
    notify: bool = True


@dataclass(frozen=True)
class SubagentInput:
    role: str
    system_prompt: str
    user_prompt: str
    parent_run_dir: str
    # Allow-list of tool names the subagent may use. Empty list = text-only.
    allowed_tool_names: list[str] = field(default_factory=list)
    max_iterations: int = 20
    tier_name: str = "HAIKU"


# ---------------------------------------------------------------------------
# Helpers (workflow-safe: no I/O, no non-deterministic calls)
# ---------------------------------------------------------------------------


def _build_system_prompt(skill_md_content: str, run_dir: str, inbox_path: str) -> str:
    """Combine SKILL.md + runtime context + tool-use instructions into a system prompt."""

    prefix = (
        "You are running as a durable Temporal workflow. The user invoked a "
        "claude-skill; its SKILL.md is included below. Use the available tools "
        "to complete the task, then end your turn when done.\n\n"
        "Runtime context:\n"
        f"- run_dir: {run_dir} (all file paths resolve under here)\n"
        f"- inbox_path: {inbox_path}\n\n"
        "Rules:\n"
        "- Call tools to do work; don't just describe what you'd do.\n"
        "- Pass RELATIVE paths to file-bearing tools (write_artifact, read_file, "
        "bash working_dir, grep path, glob root). They are auto-resolved under "
        "run_dir — any leading `/` is stripped and the path is joined under "
        f"{run_dir}.\n"
        "- The workflow writes its own summary to `run-summary.md` at the end. "
        "Use any other filename (e.g., `report.md`, `findings.md`) for skill "
        "output; `run-summary.md` is reserved.\n"
        "- For parallelizable subtasks, use spawn_subagent — it runs in its "
        "own durable child workflow.\n"
        "- If you hit an unrecoverable error, explain what happened in your "
        "final text response and end your turn.\n\n"
    )
    return prefix + "--- SKILL.md ---\n" + skill_md_content


def _initial_user_message(skill_name: str, user_args: dict[str, str]) -> str:
    """Build the initial user message summarizing the invocation."""

    if user_args:
        args_text = "\n".join(f"- {k}: {v}" for k, v in user_args.items())
    else:
        args_text = "(no args)"
    return (
        f"Run skill {skill_name!r} with the following user args:\n{args_text}\n\n"
        "Work through the SKILL.md instructions and produce the final report."
    )


def _subagent_system_prompt(inp: "SubagentInput") -> str:
    allowed = ", ".join(inp.allowed_tool_names) if inp.allowed_tool_names else "(none — text-only subagent)"
    return (
        f"You are a subagent running as a durable Temporal child workflow. "
        f"Your role is {inp.role!r}.\n"
        f"You may call only these tools: {allowed}.\n"
        f"Parent run directory (shared artifacts): {inp.parent_run_dir}\n"
        "Focus on the user prompt. When done, produce a final text response "
        "summarizing your findings and end your turn.\n\n"
        f"--- ROLE INSTRUCTIONS ---\n{inp.system_prompt}"
    )


# Per-tool field names that should resolve under run_dir before hitting the adapter.
# Claude supplies these as relative paths; without this rewrite they'd land under the
# worker's CWD instead of the run directory (surfaced by the gen-crash-test e2e).
_RUN_SCOPED_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "write_artifact": ("path",),
    "read_file": ("path", "working_dir"),
    "bash": ("working_dir",),
    "grep": ("working_dir", "path"),
    "glob": ("root",),
}


def _resolve_under_run_dir(run_dir: str, tool_name: str, args: dict) -> dict:
    """Return a copy of ``args`` with path-bearing fields rewritten under ``run_dir``.

    - Paths already under ``run_dir`` (prefix match) pass through unchanged so
      callers that fully-qualify a path (including tests) keep working.
    - All other paths (relative like ``"step1.md"`` OR absolute pointing
      elsewhere) are treated as relative to ``run_dir``: leading ``/`` is
      stripped and the path is joined under ``run_dir``. This scopes tool
      output to the run directory instead of the worker's CWD.
    - Tools without path-bearing fields pass through unchanged.
    """
    fields = _RUN_SCOPED_PATH_FIELDS.get(tool_name, ())
    out = dict(args)
    if not fields or not run_dir:
        return out
    base = run_dir.rstrip("/")
    for f in fields:
        v = out.get(f)
        if not isinstance(v, str) or not v:
            continue
        # Already under run_dir? Keep as-is. (Catches `{run_dir}/foo.md` and
        # `{run_dir}` itself, but not sibling dirs that happen to share a prefix.)
        if v == base or v.startswith(base + "/"):
            continue
        out[f] = f"{base}/{v.lstrip('/')}"
    return out


def _trim_history(messages: list[dict]) -> list[dict]:
    """Cap history length. Keep the first user turn (context anchor) + the tail.

    Anthropic's tool-use protocol requires every assistant `tool_use` block to
    be followed by a user `tool_result` reply, so we only slice on
    alternating boundaries: keep complete (assistant, user-with-tool-results)
    pairs. Falling back to simple tail-slicing is fine in our test cases since
    the fake Claude activity never enters an infinite tool-use loop that could
    produce an unpaired split — but we still guard for the boundary below to
    avoid dropping a `tool_use` without its matching `tool_result`.
    """

    if len(messages) <= _HISTORY_MAX_MESSAGES:
        return messages
    head = messages[:1]
    tail = messages[-_HISTORY_KEEP_TAIL:]
    # If the tail starts with a user tool_result message, that's orphaned — its
    # paired assistant tool_use was cut. Drop leading tool_result messages.
    while tail and _is_tool_result_message(tail[0]):
        tail = tail[1:]
    return head + tail


def _is_tool_result_message(msg: dict) -> bool:
    """Return True if `msg` is a user message whose content is tool_result blocks."""

    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _assistant_message_from_response(response: ClaudeResponse) -> dict:
    """Serialize a ClaudeResponse into an Anthropic assistant-role message.

    The assistant message must include EVERY content block that Claude
    emitted (text + tool_use) so the next user message's `tool_result`
    blocks have matching `tool_use_id`s.
    """

    content: list[dict] = []
    if response.text:
        content.append({"type": "text", "text": response.text})
    for tu in response.tool_uses:
        content.append(
            {
                "type": "tool_use",
                "id": tu.id,
                "name": tu.name,
                "input": dict(tu.input),
            }
        )
    if not content:
        # Claude returned no content at all; use a placeholder so the
        # conversation structure stays valid.
        content = [{"type": "text", "text": ""}]
    return {"role": "assistant", "content": content}


def _tool_result_message(tool_results: list[dict]) -> dict:
    """Wrap tool_result blocks into a single user-role message."""

    return {"role": "user", "content": tool_results}


def _format_tool_result(content: str) -> str:
    """Stringify an arbitrary Python result for the tool_result content field.

    Anthropic's tool-use API accepts the `content` of a tool_result as either
    a string or a list of content blocks. We use strings — simpler and
    sufficient for our tools.
    """

    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, default=str)
    except (TypeError, ValueError):
        return str(content)


# ---------------------------------------------------------------------------
# ClaudeSkillWorkflow
# ---------------------------------------------------------------------------


@workflow.defn(name="ClaudeSkillWorkflow")
class ClaudeSkillWorkflow:
    """Top-level coordinator. Runs Claude's tool-use loop with the full tool palette."""

    @workflow.run
    async def run(self, inp: ClaudeSkillInput) -> str:
        system_prompt = _build_system_prompt(
            inp.skill_md_content, inp.run_dir, inp.inbox_path
        )
        messages: list[dict] = [
            {"role": "user", "content": _initial_user_message(inp.skill_name, inp.user_args)}
        ]

        last_text = ""
        truncated = False
        iteration = 0
        subagent_counter = 0

        while iteration < inp.max_iterations:
            iteration += 1
            response: ClaudeResponse = await workflow.execute_activity(
                "call_claude_with_tools",
                CallClaudeInput(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=ALL_TOOLS,
                    tier_name=inp.tier_name,
                    max_tokens=_CLAUDE_MAX_TOKENS,
                ),
                result_type=ClaudeResponse,
                start_to_close_timeout=timedelta(seconds=300),
                heartbeat_timeout=timedelta(seconds=90),
                retry_policy=SONNET_POLICY,
            )
            if response.text:
                last_text = response.text

            if response.stop_reason == "end_turn":
                break
            if response.stop_reason == "max_tokens":
                # Claude hit its output cap mid-turn. Preserve what we have and exit.
                break
            if not response.tool_uses:
                # No tool_uses but not end_turn either — defensively break to avoid loop.
                break

            # Append the assistant turn before dispatching tool_uses so the
            # next message's tool_result ids match.
            messages.append(_assistant_message_from_response(response))

            # Dispatch all tool_uses in parallel. Each becomes its own Temporal
            # activity (or child workflow) event in the history.
            results = await asyncio.gather(
                *[
                    self._dispatch_tool(
                        tu,
                        parent_run_id=inp.run_id,
                        run_dir=inp.run_dir,
                        sub_index=subagent_counter + i,
                    )
                    for i, tu in enumerate(response.tool_uses)
                ]
            )
            # Bump the subagent counter by the number of spawn_subagent calls we
            # just dispatched so child workflow ids stay unique.
            subagent_counter += sum(
                1 for tu in response.tool_uses if tu.name == "spawn_subagent"
            )

            tool_result_blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": _format_tool_result(result),
                }
                for tu, result in zip(response.tool_uses, results)
            ]
            messages.append(_tool_result_message(tool_result_blocks))

            # Keep history bounded so long runs don't blow out token budget.
            messages = _trim_history(messages)

        if iteration >= inp.max_iterations:
            truncated = True

        # Build and persist final report.
        report_body = _build_final_report(
            skill_name=inp.skill_name,
            run_id=inp.run_id,
            iterations=iteration,
            max_iterations=inp.max_iterations,
            truncated=truncated,
            final_text=last_text,
        )
        report_path = f"{inp.run_dir}/run-summary.md"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=report_path, content=report_body),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        # Emit finding to INBOX.
        summary = last_text.strip().splitlines()[0] if last_text.strip() else f"{inp.skill_name} completed"
        if truncated:
            summary = f"[max_iterations reached] {summary}"
        status = "TRUNCATED" if truncated else "DONE"
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill=inp.skill_name,
                status=status,
                summary=summary[:500],
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return last_text

    async def _dispatch_tool(
        self,
        tu: ClaudeToolUse,
        *,
        parent_run_id: str,
        run_dir: str,
        sub_index: int,
    ) -> str:
        """Dispatch one tool_use block to its handler. Returns a stringifiable result."""

        return await _dispatch_tool_use(
            tu,
            parent_run_id=parent_run_id,
            parent_run_dir_for_subagent=run_dir,  # parent's run_dir is the subagent's base
            run_dir=run_dir,
            sub_index=sub_index,
            allowed_tool_names=None,  # coordinator: everything allowed
        )


# ---------------------------------------------------------------------------
# SubagentWorkflow (child)
# ---------------------------------------------------------------------------


@workflow.defn(name="SubagentWorkflow")
class SubagentWorkflow:
    """Child workflow for spawn_subagent. Runs the tool-use loop with a filtered tool list."""

    @workflow.run
    async def run(self, inp: SubagentInput) -> str:
        # Filter the tool palette to what the subagent is allowed to call.
        allowed_names = set(inp.allowed_tool_names or [])
        subagent_tools = [t for t in ALL_TOOLS if t["name"] in allowed_names]
        system_prompt = _subagent_system_prompt(inp)
        messages: list[dict] = [{"role": "user", "content": inp.user_prompt}]

        last_text = ""
        iteration = 0

        while iteration < inp.max_iterations:
            iteration += 1
            response: ClaudeResponse = await workflow.execute_activity(
                "call_claude_with_tools",
                CallClaudeInput(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=subagent_tools,
                    tier_name=inp.tier_name,
                    max_tokens=_CLAUDE_MAX_TOKENS,
                ),
                result_type=ClaudeResponse,
                start_to_close_timeout=timedelta(seconds=300),
                heartbeat_timeout=timedelta(seconds=90),
                retry_policy=HAIKU_POLICY,
            )
            if response.text:
                last_text = response.text

            if response.stop_reason in ("end_turn", "max_tokens"):
                break
            if not response.tool_uses:
                break

            messages.append(_assistant_message_from_response(response))

            # Dispatch in parallel, with the subagent's allow-list enforced.
            results = await asyncio.gather(
                *[
                    _dispatch_tool_use(
                        tu,
                        parent_run_id=workflow.info().workflow_id,
                        parent_run_dir_for_subagent=inp.parent_run_dir,
                        run_dir=inp.parent_run_dir,
                        sub_index=i,
                        allowed_tool_names=allowed_names,
                    )
                    for i, tu in enumerate(response.tool_uses)
                ]
            )

            tool_result_blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": _format_tool_result(result),
                }
                for tu, result in zip(response.tool_uses, results)
            ]
            messages.append(_tool_result_message(tool_result_blocks))
            messages = _trim_history(messages)

        return last_text


# ---------------------------------------------------------------------------
# Tool dispatch (shared by ClaudeSkillWorkflow + SubagentWorkflow)
# ---------------------------------------------------------------------------


async def _dispatch_tool_use(
    tu: ClaudeToolUse,
    *,
    parent_run_id: str,
    parent_run_dir_for_subagent: str,
    run_dir: str,
    sub_index: int,
    allowed_tool_names: set[str] | None,
) -> str:
    """Execute one tool_use block and return a string result for tool_result.content.

    ``allowed_tool_names is None`` means no filter (coordinator context). An
    empty set or a set that doesn't contain ``tu.name`` results in a
    'tool not allowed' error string returned to Claude — so Claude can
    re-route rather than crashing the workflow.
    """

    if allowed_tool_names is not None and tu.name not in allowed_tool_names:
        return (
            f"error: tool {tu.name!r} is not allowed for this subagent. "
            f"Allowed tools: {sorted(allowed_tool_names) or 'none'}"
        )

    handler = TOOL_HANDLERS.get(tu.name)
    if handler is None:
        return f"error: unknown tool {tu.name!r}"

    # Child-workflow dispatch (spawn_subagent).
    if "handler_child_workflow" in handler:
        return await _dispatch_spawn_subagent(
            tu,
            parent_run_id=parent_run_id,
            parent_run_dir_hint=parent_run_dir_for_subagent,
            sub_index=sub_index,
        )

    # Activity dispatch: pack args into an `input_dict` and hand to an adapter
    # activity. The adapter activity (registered alongside the tool handlers)
    # reconstructs the typed dataclass before invoking the real handler. We
    # avoid running the dataclass ctor directly in the workflow because
    # dataclasses + optional fields aren't guaranteed deterministic across
    # Temporal replays when the schema evolves.
    activity_name = handler["handler_activity"]
    start_to_close, heartbeat = _TOOL_ACTIVITY_TIMEOUTS.get(activity_name, (30, 30))
    try:
        kwargs = {
            "start_to_close_timeout": timedelta(seconds=start_to_close),
            "retry_policy": HAIKU_POLICY,
        }
        if heartbeat:
            kwargs["heartbeat_timeout"] = timedelta(seconds=heartbeat)
        # Rewrite path-bearing fields so tool-level relative paths resolve under
        # run_dir, not the worker's CWD. Tools without file paths pass through.
        resolved_args = _resolve_under_run_dir(run_dir, tu.name, dict(tu.input))
        result = await workflow.execute_activity(
            f"generic_tool_adapter__{activity_name}",
            resolved_args,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        return f"error: tool {tu.name!r} failed: {exc}"
    return _format_tool_result(result)


async def _dispatch_spawn_subagent(
    tu: ClaudeToolUse,
    *,
    parent_run_id: str,
    parent_run_dir_hint: str,
    sub_index: int,
) -> str:
    args = dict(tu.input or {})
    role = str(args.get("role") or "subagent")
    system_prompt = str(args.get("system_prompt") or "")
    user_prompt = str(args.get("user_prompt") or "")
    tools = args.get("tools") or []
    if not isinstance(tools, list):
        tools = []
    tools_clean = [str(t) for t in tools]
    tier_name = str(args.get("tier_name") or "HAIKU")
    max_iter = args.get("max_iterations")
    try:
        max_iter_int = int(max_iter) if max_iter is not None else 20
    except (TypeError, ValueError):
        max_iter_int = 20
    max_iter_int = max(1, min(max_iter_int, 200))

    child_input = SubagentInput(
        role=role,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        parent_run_dir=parent_run_dir_hint,
        allowed_tool_names=tools_clean,
        max_iterations=max_iter_int,
        tier_name=tier_name,
    )
    try:
        result = await workflow.execute_child_workflow(
            SubagentWorkflow.run,
            child_input,
            id=f"{parent_run_id}-sub-{sub_index}",
            task_queue=TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001
        return f"error: subagent {role!r} failed: {exc}"
    return _format_tool_result(result)


# ---------------------------------------------------------------------------
# Final report assembly
# ---------------------------------------------------------------------------


def _build_final_report(
    *,
    skill_name: str,
    run_id: str,
    iterations: int,
    max_iterations: int,
    truncated: bool,
    final_text: str,
) -> str:
    lines = [
        f"# {skill_name} — run {run_id}",
        "",
        f"**Iterations:** {iterations} / {max_iterations}",
    ]
    if truncated:
        lines.append(
            "**NOTE:** terminated at max_iterations — the conversation may be incomplete."
        )
    lines += ["", "## Final output", "", final_text.strip() or "(no final text)"]
    return "\n".join(lines)


__all__ = [
    "ClaudeSkillInput",
    "ClaudeSkillWorkflow",
    "SubagentInput",
    "SubagentWorkflow",
]
