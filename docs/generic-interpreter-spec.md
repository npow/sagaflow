# sagaflow generic interpreter — architecture spec

Single Python workflow that runs any claude-skills `SKILL.md` durably on Temporal. Claude is the step-by-step coordinator (via Anthropic API tool-use); the workflow loops, dispatching each of Claude's tool requests as its own Temporal activity (or child workflow for subagents). Step-level durability: a crash at any point resumes from the last completed activity in any workflow or sub-workflow in the tree.

## Module layout

```
sagaflow/
  generic/
    __init__.py
    tools.py          # Anthropic tool schemas (JSON) + dispatch table
    activities.py     # Temporal activities implementing the tools + call_claude_with_tools
    workflow.py       # ClaudeSkillWorkflow + SubagentWorkflow (child)
    state.py          # ClaudeSkillInput / SubagentInput / conversation types

skills/
  generic/
    __init__.py       # registers the generic workflow + build_input
```

## Tool schemas (`sagaflow/generic/tools.py`)

Each tool = a JSON schema passed to the Anthropic API and a `handler_activity_name` the workflow dispatches when Claude invokes it.

```python
WRITE_ARTIFACT_TOOL = {
    "name": "write_artifact",
    "description": "Write content to a file in the run directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to run_dir"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    "handler_activity": "write_artifact",  # existing activity
}

READ_FILE_TOOL = {...}     # handler_activity: "read_file_tool"
BASH_TOOL = {...}          # handler_activity: "bash_tool"
GREP_TOOL = {...}          # handler_activity: "grep_tool"
GLOB_TOOL = {...}          # handler_activity: "glob_tool"
SPAWN_SUBAGENT_TOOL = {    # dispatches as CHILD WORKFLOW, not activity
    "name": "spawn_subagent",
    "description": "Spawn a subagent to work on a focused subtask...",
    "input_schema": {...},
    "handler_child_workflow": "SubagentWorkflow",
}

ALL_TOOLS = [WRITE_ARTIFACT_TOOL, READ_FILE_TOOL, BASH_TOOL, GREP_TOOL, GLOB_TOOL, SPAWN_SUBAGENT_TOOL]
```

## Activities (`sagaflow/generic/activities.py`)

### `call_claude_with_tools`

```python
@dataclass(frozen=True)
class CallClaudeInput:
    system_prompt: str               # SKILL.md + runtime context
    messages: list[dict]             # conversation history, alternating user/assistant
    tools: list[dict]                # tool schemas
    tier_name: str                   # "HAIKU" | "SONNET" | "OPUS"
    max_tokens: int = 4096

@dataclass(frozen=True)
class ClaudeResponse:
    content_blocks: list[dict]       # text + tool_use blocks from Claude
    stop_reason: str                 # "end_turn" | "tool_use" | "max_tokens"
    input_tokens: int
    output_tokens: int

@activity.defn(name="call_claude_with_tools")
async def call_claude_with_tools(inp: CallClaudeInput) -> ClaudeResponse: ...
```

Implementation: hits Anthropic API with `tools=inp.tools`. Extracts text + tool_use content blocks. Background heartbeat loop (reuse existing pattern from `spawn_subagent`).

### Tool-handler activities

```python
@activity.defn(name="read_file_tool")
async def read_file_tool(inp: ReadFileInput) -> str: ...

@activity.defn(name="bash_tool")
async def bash_tool(inp: BashInput) -> BashResult: ...

@activity.defn(name="grep_tool")
async def grep_tool(inp: GrepInput) -> GrepResult: ...

@activity.defn(name="glob_tool")
async def glob_tool(inp: GlobInput) -> list[str]: ...
```

All with heartbeats, sensible timeouts, scoped to the run_dir or the working repo.

## Workflows (`sagaflow/generic/workflow.py`)

### `ClaudeSkillWorkflow`

```python
@dataclass(frozen=True)
class ClaudeSkillInput:
    run_id: str
    run_dir: str
    inbox_path: str
    skill_name: str                  # e.g., "deep-idea"
    skill_md_content: str            # loaded from ~/.claude/skills/<skill_name>/SKILL.md
    user_args: dict[str, str]        # CLI args from `sagaflow launch`
    max_iterations: int = 50         # hard cap on loop iterations
    notify: bool = True

@workflow.defn(name="ClaudeSkillWorkflow")
class ClaudeSkillWorkflow:
    @workflow.run
    async def run(self, inp: ClaudeSkillInput) -> str:
        # 1. Build initial messages from skill_md_content + user_args
        # 2. Tool-use loop:
        #    while iterations < max_iterations:
        #        response = execute_activity("call_claude_with_tools", ...)
        #        if response.stop_reason == "end_turn": break
        #        tool_results = await asyncio.gather(*[
        #            self._dispatch_tool(tu) for tu in response.tool_uses
        #        ])
        #        messages.append(assistant_msg); messages.append(user_msg(tool_results))
        # 3. Write final report to run_dir/report.md
        # 4. emit_finding + return
```

### `SubagentWorkflow` (child)

```python
@dataclass(frozen=True)
class SubagentInput:
    role: str                        # "critic" | "judge" | whatever Claude decides
    system_prompt: str               # subagent instructions
    user_prompt: str                 # the subtask
    parent_run_dir: str              # where to write subagent artifacts
    tools: list[str]                 # subset of tool names this subagent may use
    max_iterations: int = 20
    tier_name: str = "HAIKU"

@workflow.defn(name="SubagentWorkflow")
class SubagentWorkflow:
    @workflow.run
    async def run(self, inp: SubagentInput) -> str:
        # Same tool-use loop, scoped to subagent's tools. Returns final text or
        # structured output parsed from the last assistant message.
```

Dispatched from `ClaudeSkillWorkflow` via `workflow.start_child_workflow` (not `execute_activity`) so the subagent's own tool-use steps are each durable in its own child event history.

## CLI dispatch (`sagaflow/cli.py`)

```python
def _start_workflow(skill: str, args: dict) -> str:
    registry = build_registry()
    try:
        spec = registry.get(skill)
    except KeyError:
        # Fall back to generic interpreter if the skill exists in claude-skills.
        claude_skill_path = claude_skills_dir() / skill / "SKILL.md"
        if claude_skill_path.exists():
            spec = registry.get("generic")
            args = {**args, "_target_skill": skill}
        else:
            raise click.UsageError(f"unknown skill {skill!r}; no SKILL.md at {claude_skill_path}")
    # ... existing dispatch flow from here
```

`skills/generic/_build_input` reads `_target_skill` from args, loads its SKILL.md, builds a `ClaudeSkillInput`.

## Tests

### Unit (`tests/generic/`)

- `test_tools.py` — validate tool schemas, registry, `bash_tool` happy/error paths, `read_file_tool` missing path, etc.
- `test_call_claude_with_tools.py` — mock Anthropic SDK; assert tools are passed through; tool_use content blocks are extracted correctly; stop_reason preserved.
- `test_workflow_unit.py` — mock activities; drive the ClaudeSkillWorkflow through a simulated conversation ending in `end_turn`.

### Integration (time-skipping env)

- `test_claude_skill_workflow_round_trips.py` — real Temporal test env, fake `call_claude_with_tools` that emits a scripted sequence of tool_uses; assert the workflow dispatches them correctly in parallel and terminates.
- `test_subagent_child_workflow.py` — parent dispatches SubagentWorkflow as child; asserts child runs its own loop and returns.
- `test_crash_recovery.py` — time-skipping env can't truly crash a worker, but we can verify workflow replay idempotence with an activity that records invocation count — asserting each activity is called exactly once on completion.

### E2E (real Temporal, real LLM)

- `test_e2e_deep_idea.py` (or any simple claude-skill without a bespoke Python workflow): launch via `sagaflow launch <skill>`, assert INBOX + report.md appear.
- Manual: kill worker mid-run, confirm resume on restart.

## Registry change

`sagaflow/worker.py::build_registry()` gains `from skills.generic import register as register_generic; register_generic(registry)` so `registry.get("generic")` resolves.

## Loop / done criteria

Build done when:
1. All unit tests pass.
2. All integration tests pass (time-skipping env).
3. E2E: `sagaflow launch <claude-skill>` succeeds end-to-end for a claude-skill without bespoke Python, producing a real report.
4. Crash-recovery: worker killed mid-run, restarted; workflow resumes; final report written.
5. Full regression suite (existing sagaflow tests) still green.
