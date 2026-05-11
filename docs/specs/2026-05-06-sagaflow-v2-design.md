# Sagaflow v2: Output-style API on Pydantic AI + Temporal

**Date:** 2026-05-06
**Status:** Design

## Problem

Sagaflow grew bottom-up: Temporal plumbing first, skill API bolted on. Making a
skill "sagaflow compatible" requires Temporal internals knowledge (workflow.defn,
execute_activity, unsafe imports, retry policies, prompt file management). The
new `@workflow`/`generate_text` API hides this, but it reimplements what Pydantic
AI's Temporal integration already provides — and misses sagaflow's own internal
features (budget, signals, replay, portfolio).

## Decision

Rebase sagaflow's execution layer on **Pydantic AI + Temporal** (`pydantic-ai[temporal]`).
Keep sagaflow's unique features as the value-add layer on top. Follow Output.ai's
developer experience (`.prompt` files, `workflow()`, `generate_text()`, evaluators).

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Skill author writes:                            │
│    workflow.py  +  prompts/*.prompt               │
│    (sagaflow public API — no Temporal knowledge)  │
├──────────────────────────────────────────────────┤
│  sagaflow API layer                              │
│    @workflow, generate_text(), parallel(),         │
│    write_file(), progress(), evaluator()           │
│    .prompt file loading + templating              │
│    Auto-registration + CLI arg parsing            │
├──────────────────────────────────────────────────┤
│  sagaflow value-add layer                        │
│    Budget enforcement (BudgetEnforcer)            │
│    Behavioral signals + regression detection      │
│    Run manifest + tracing                        │
│    Replay cassettes                              │
│    Portfolio analytics                           │
│    Skill discovery + SKILL.md generic interpreter │
│    Intervention (pause/resume/abort)             │
│    Slack progress + artifact delivery            │
├──────────────────────────────────────────────────┤
│  Pydantic AI + Temporal                          │
│    TemporalAgent wraps LLM calls as Activities   │
│    Tools → Temporal Activities (auto-retry)       │
│    Structured outputs via Pydantic models         │
│    Observability via OpenTelemetry / Logfire      │
│    Multi-model support (Anthropic, OpenAI, etc.)  │
├──────────────────────────────────────────────────┤
│  Temporal                                        │
│    Durable execution, replay, heartbeats          │
│    Workflow history, signals, queries             │
└──────────────────────────────────────────────────┘
```

## What changes

### Replaced by Pydantic AI

| sagaflow today | Pydantic AI replacement |
|---|---|
| `transport/anthropic_sdk.py` | Pydantic AI `AnthropicModel` |
| `transport/claude_cli.py` | Pydantic AI `Agent` with MCP tools |
| `transport/dispatcher.py` | `TemporalAgent.run()` |
| `spawn_subagent` activity | Pydantic AI model request Activity |
| `SpawnSubagentInput` dataclass | Pydantic AI serialization |
| Custom retry policies | Temporal `ActivityConfig` |
| Structured output parsing (`parse_structured`) | Pydantic model output |
| Malformed response handling | Pydantic AI retry + validation |
| Heartbeat loop | Pydantic AI built-in |

### Kept (sagaflow unique value)

| Feature | Why keep |
|---|---|
| Budget enforcement | No equivalent in Pydantic AI |
| Behavioral signals + regression detection | Custom analytics |
| Run manifest + cost tracking | Richer than Logfire for our use |
| Replay cassettes | Deterministic test replay |
| Portfolio analytics (ROI scorer) | Custom business logic |
| Skill discovery + SKILL.md interpreter | Unique to sagaflow |
| Intervention (pause/resume) | Temporal signals, custom |
| Slack progress + artifact delivery | Custom integration |
| `.prompt` file format | Output-style DX |
| `@workflow` / `generate_text()` API | Higher-level than Pydantic AI |

### Kept but rewired

| Feature | Change |
|---|---|
| `write_artifact` activity | Keep — not an LLM call |
| `emit_finding` activity | Keep — inbox is custom |
| `run_shell` activity | Keep — subprocess execution |
| `finalize_manifest` activity | Keep — custom manifest |
| `report_slack_progress` activity | Keep — Slack integration |

## Public API (what skill authors see)

### workflow.py

```python
from sagaflow import workflow, generate_text, parallel, write_file, progress

@workflow(name="deep-code-review", phases=["Critique", "Judge", "Synthesize"])
async def run(task: str, max_rounds: int = 3):
    await progress(0, "dispatching critics")
    results = await parallel(
        generate_text("security-critic", variables={"task": task}),
        generate_text("perf-critic", variables={"task": task}),
    )

    await progress(1, "judging")
    verdict = await generate_text("judge", variables={"findings": str(results)})

    await progress(2, "synthesizing")
    report = await generate_text("synthesizer", variables={"verdict": str(verdict)})
    await write_file("report.md", report.get("REPORT", ""))

    return f"{len(results)} critics, verdict: {verdict.get('SEVERITY', 'unknown')}"
```

### prompts/security-critic.prompt

```yaml
---
tier: HAIKU
max_tokens: 64000
---
<system>
You are a security reviewer. Find vulnerabilities, injection risks,
auth bypasses, and data exposure.
</system>
<user>
Review this code for security issues:

$task
</user>
```

### What the author does NOT write

- No `__init__.py` (auto-discovered)
- No `state.py` (state managed by Temporal)
- No Temporal decorators
- No activity dispatch code
- No retry/timeout configuration
- No prompt file management
- No manifest finalization
- No inbox emission
- No Slack delivery

## Implementation plan

### Phase 1: Pydantic AI foundation

1. Add `pydantic-ai[temporal]` dependency
2. Create `sagaflow/engine.py` — wraps Pydantic AI `TemporalAgent`
3. Rewire `generate_text()` to use Pydantic AI instead of `spawn_subagent`
4. Keep all existing activities (write, emit, shell, progress, manifest)
5. Worker registers Pydantic AI plugin + existing activities

### Phase 2: Wire sagaflow features through API

6. Budget enforcement → `@workflow(budget_usd=25.0)` parameter
7. Cost tracking → `generate_text()` returns cost metadata
8. Intervention → `@workflow(allow_intervention=True)` adds signal handlers
9. Evaluators → `evaluator()` decorator using Pydantic AI structured output
10. Tracing → OpenTelemetry + Logfire integration via PydanticAIPlugin

### Phase 3: Migration

11. Migrate existing deep-* skills to new API one at a time
12. Keep old `durable/helpers.py` working for backward compatibility
13. Deprecate direct `spawn_subagent` usage in skill code
14. Update SKILL.md generic interpreter to use Pydantic AI engine

### Phase 4: Feature parity with Output.ai

15. Evaluator framework (LLM-as-judge with confidence scores)
16. HTTP client wrapper with tracing
17. Encrypted credentials (per-environment secrets)
18. `sagaflow dev` command (starts Temporal + worker + watches files)
19. Web dashboard for run history, cost, and tracing

## Limitations

- Pydantic AI's `run_stream()` not supported in Temporal workflows (Activities can't stream)
  - Workaround: Temporal Workflow Streams (public preview) or event_stream_handler
- Agent names must be stable once deployed (Activity name = agent name)
- 2MB Temporal payload limit for Activity inputs/outputs
- Dependencies passed to agents must be Pydantic-serializable

## Open questions

1. Should `generate_text()` return a Pydantic model instead of `dict[str, str]`?
   Argument for: type safety, validation. Against: breaking change, dict is simpler.

2. Should we keep the `Skill` base class alongside `@workflow` decorator?
   Argument for: class-based is more natural for stateful workflows. Against: two APIs is confusing.

3. How to handle the `tools_needed=True` case (Claude CLI with MCP)?
   Pydantic AI has MCP support but not via Claude CLI subprocess.
   May need to keep `ClaudeCliTransport` for tool-using agents.
