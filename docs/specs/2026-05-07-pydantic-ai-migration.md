# Sagaflow v2: Pydantic AI Migration + Venv Decoupling

**Date:** 2026-05-07
**Status:** Design (supersedes 2026-05-06-sagaflow-v2-design.md)
**Approach:** B — Pydantic AI for SDK path, keep CLI transport

## Problem

Sagaflow's transport layer reimplements what Pydantic AI's Temporal integration
provides: wrapping LLM calls as Temporal activities, retry, structured output,
serialization. Meanwhile, sagaflow is installed into BDI system venvs, polluting
shared environments and making dependency management fragile.

## Decisions

1. **Pydantic AI for SDK path only.** Non-tool-using LLM calls (~90% of traffic)
   go through `pydantic_ai.Agent` → `TemporalAgent`. Tool-using agents keep the
   `ClaudeCliTransport` subprocess path — Pydantic AI's MCP support can't replicate
   the full Claude Code toolbelt.

2. **Drop the manifest package.** The `sagaflow/manifest/` directory from the stash
   is not useful for v2. Run tracking stays in `run_manifest.py`.

3. **Own venv via uv.** Sagaflow gets a project-local `.venv` managed by `uv`.
   The dotfiles installer creates it and symlinks the CLI to `~/bin/`.

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Skill author writes:                            │
│    workflow.py  +  prompts/*.prompt               │
│    (sagaflow public API — no Temporal knowledge)  │
├──────────────────────────────────────────────────┤
│  sagaflow API layer                              │
│    @workflow, generate_text(), parallel(),         │
│    write_file(), progress()                       │
│    .prompt file loading + templating              │
├──────────────────────────────────────────────────┤
│  sagaflow engine (NEW)                           │
│    SdkEngine: pydantic_ai Agent → TemporalAgent  │
│    CliEngine: ClaudeCliTransport (unchanged)      │
│    generate_text() routes by tools_needed flag    │
├──────────────────────────────────────────────────┤
│  sagaflow value-add layer                        │
│    Budget enforcement, behavioral signals,        │
│    run manifest, replay cassettes,                │
│    skill discovery, Slack integration             │
├──────────────────────────────────────────────────┤
│  Pydantic AI + Temporal                          │
│    TemporalAgent wraps LLM calls as Activities   │
│    Structured outputs via Pydantic models         │
│    Auto-retry via ActivityConfig                  │
│    PydanticAIPlugin for worker registration       │
├──────────────────────────────────────────────────┤
│  Temporal                                        │
│    Durable execution, replay, heartbeats          │
└──────────────────────────────────────────────────┘
```

## Phase 0: Venv + Installer Decoupling

### Changes

1. **`installers/11-sagaflow.sh`** — rewrite:
   - If `~/code/sagaflow` exists (dev machine): `uv venv ~/code/sagaflow/.venv`
     then `uv pip install -e ~/code/sagaflow` into that venv
   - Else (non-dev): `uv tool install sagaflow` (installs from PyPI into
     uv's tool directory with its own isolated venv)
   - Create `~/bin/sagaflow` symlink pointing to the venv's entrypoint
   - Remove all references to `/apps/bdi-venv-*`

2. **`~/bin/` on PATH** — the dotfiles shell profile (`zshrc` or `bashrc`)
   already adds `~/bin` to PATH. Verify this; add if missing.

3. **Worker startup** — `sagaflow worker run` invoked via the symlink
   resolves to the venv's Python, so all deps (including pydantic-ai) are
   available without activating the venv.

4. **`pyproject.toml`** — add dependency:
   ```
   "pydantic-ai[anthropic,temporal]>=1.80.0",
   ```

## Phase 1: Engine Layer

### New file: `sagaflow/engine.py`

Central dispatch that replaces `transport/dispatcher.py`.

```python
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalAgent, ActivityConfig
from pydantic import BaseModel
from datetime import timedelta

TIER_TO_MODEL = {
    "HAIKU": "anthropic:claude-haiku-4-5-20251001",
    "SONNET": "anthropic:claude-sonnet-4-6",
    "OPUS": "anthropic:claude-opus-4-7",
}

# Cache of TemporalAgent instances keyed by (name, tier, system_prompt hash)
_agent_cache: dict[str, TemporalAgent] = {}

def get_sdk_agent(
    name: str,
    tier: str,
    system_prompt: str,
    output_type: type[BaseModel] | None = None,
    max_tokens: int = 128_000,
) -> TemporalAgent:
    """Get or create a TemporalAgent for the given config."""
    cache_key = f"{name}:{tier}"
    if cache_key in _agent_cache:
        return _agent_cache[cache_key]

    model = TIER_TO_MODEL.get(tier, TIER_TO_MODEL["SONNET"])
    agent = Agent(
        model,
        name=name,
        instructions=system_prompt,
        output_type=output_type or str,
    )
    temporal_agent = TemporalAgent(
        agent,
        name=name,
        activity_config=ActivityConfig(
            start_to_close_timeout=timedelta(minutes=15),
        ),
    )
    _agent_cache[cache_key] = temporal_agent
    return temporal_agent


async def run_sdk(
    name: str,
    tier: str,
    system_prompt: str,
    user_prompt: str,
    output_type: type[BaseModel] | None = None,
    max_tokens: int = 128_000,
) -> dict[str, str]:
    """Run an LLM call via Pydantic AI TemporalAgent (inside a workflow)."""
    agent = get_sdk_agent(name, tier, system_prompt, output_type, max_tokens)
    result = await agent.run(user_prompt)
    if isinstance(result.output, BaseModel):
        return result.output.model_dump()
    return {"RESPONSE": str(result.output)}


async def run_cli(
    prompt: str,
    timeout_seconds: float = 3600.0,
    model: str | None = None,
    label: str = "",
    mcp_config_path: str | None = None,
) -> dict[str, str]:
    """Run an LLM call via Claude CLI subprocess (existing transport)."""
    # Delegates to existing ClaudeCliTransport via Temporal activity
    ...
```

### Mapping

| Current | v2 replacement |
|---|---|
| `transport/anthropic_sdk.py` AnthropicSdkTransport | `engine.py` get_sdk_agent() → Pydantic AI Agent |
| `transport/dispatcher.py` dispatch_subagent() | `engine.py` run_sdk() / run_cli() |
| `transport/structured_output.py` parse_structured() | Pydantic AI output_type (SDK path) |
| ModelTier enum | TIER_TO_MODEL dict |
| Custom retry loop in AnthropicSdkTransport | Pydantic AI + Temporal ActivityConfig retry |

### Deleted files

- `sagaflow/transport/anthropic_sdk.py`
- `sagaflow/transport/dispatcher.py`
- `sagaflow/transport/structured_output.py` (SDK path no longer needs regex parsing)
- `sagaflow/manifest/` (entire directory — not useful for v2)

### Kept files

- `sagaflow/transport/claude_cli.py` — tool-using agent subprocess
- `sagaflow/transport/boundary.py` — injection detection (still needed for CLI output)

## Phase 2: Wire generate_text() Through Engine

### Changes to `sagaflow/api.py`

`generate_text()` currently calls `helpers.spawn_with_prompt()` which goes through
the old `spawn_subagent` activity → dispatcher → AnthropicSdkTransport. Replace:

```python
async def generate_text(prompt, *, variables=None, tier=None, tools_needed=None, ...):
    config = _resolve_prompt(prompt, ctx.prompts_dir)
    user_text = _render_template(config.user_template, variables)

    if effective_tools:
        # CLI path — unchanged, still uses ClaudeCliTransport via activity
        return await _run_cli_agent(role, effective_system, user_text, ...)
    else:
        # SDK path — NEW: Pydantic AI TemporalAgent
        from sagaflow.engine import run_sdk
        return await run_sdk(
            name=role,
            tier=effective_tier,
            system_prompt=effective_system,
            user_prompt=user_text,
            output_type=_schema_to_model(output_schema) if output_schema else None,
            max_tokens=effective_max_tokens,
        )
```

### Changes to `sagaflow/durable/helpers.py`

- `spawn_with_prompt()` and `spawn()` become thin wrappers that route through
  the engine instead of directly calling `workflow.execute_activity("spawn_subagent")`
- `spawn_parallel()` uses the same routing
- Non-LLM helpers (`write`, `emit`, `report_progress`, `finalize`) unchanged

### Changes to `sagaflow/durable/activities.py`

- `spawn_subagent` activity simplified: only handles the CLI path now
  (SDK path is handled by PydanticAIPlugin auto-generated activities)
- Remove `AnthropicSdkTransport` and `dispatch_subagent` imports

## Phase 3: Worker Registration

### Changes to `sagaflow/worker.py`

```python
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin

# In run_worker():
worker = Worker(
    client,
    task_queue=TASK_QUEUE,
    workflows=[...],
    activities=[...],  # non-LLM activities + CLI spawn activity
    plugins=[PydanticAIPlugin()],  # auto-registers SDK agent activities
    workflow_runner=_build_sandbox_runner(),
)
```

- Add `"pydantic_ai"` to `_PASSTHROUGH_MODULES`
- Workflow classes that use SDK agents declare `__pydantic_ai_agents__`
  listing the TemporalAgent instances
- The `ApiWorkflow` class (shared Temporal workflow for @workflow-decorated
  functions) gains a `__pydantic_ai_agents__` property that returns all
  agents created during registration

## Phase 4: Skill Migration

Existing skills that import `sagaflow.durable.helpers` continue working —
the helpers route through the engine. No skill code changes required for
Phase 1-3.

Optional per-skill improvements:
- Skills can define Pydantic `output_type` models instead of relying on
  dict[str, str] parsing
- Skills can use `Agent` directly for complex multi-turn flows
- `.prompt` files gain optional `output_type:` frontmatter field

## What Does NOT Change

- `.prompt` file format
- `@workflow` decorator API
- `generate_text()`, `parallel()`, `write_file()`, `progress()` signatures
- `Skill` base class
- Budget enforcement
- Behavioral signals + regression detection
- Run manifest
- Replay cassettes
- Slack progress + artifact delivery
- `finalize()` helper
- Inbox / emit_finding

## Limitations

- `TemporalAgent.run()` only — no streaming inside workflows (Temporal constraint)
- Agent names must be stable once deployed (activity name = agent name)
- 2MB Temporal payload limit for activity inputs/outputs
- `output_type` must be Pydantic-serializable
- CLI transport still needed for tool-using agents (no Pydantic AI replacement yet)
- Agent cache keyed by name+tier means changing system_prompt for the same
  name requires cache invalidation (addressed by including prompt hash in key)

## Open Questions

1. **output_type for existing skills** — Should `generate_text()` return
   `dict[str, str]` (backward compat) or the Pydantic model directly?
   Decision: return dict by default, add `raw=True` param to get the model.

2. **Model Gateway routing** — Current code uses `ANTHROPIC_BASE_URL` env var.
   Pydantic AI's `AnthropicModel` also honors this. Verify it works with
   the Netflix Model Gateway proxy URL format.

3. **Cost tracking** — Pydantic AI tracks token usage via `result.usage()`.
   Wire this into sagaflow's existing cost tracking / run manifest.
