# Pydantic AI Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace sagaflow's custom transport/dispatcher LLM layer with Pydantic AI's `Agent` + `TemporalAgent` for non-tool-using calls, and decouple sagaflow from BDI system venvs.

**Architecture:** SDK path (90% of calls) routes through `pydantic_ai.Agent` wrapped in `TemporalAgent` — auto-registered as Temporal activities via `PydanticAIPlugin`. CLI path (tool-using agents) keeps `ClaudeCliTransport` unchanged. New `sagaflow/engine.py` is the single dispatch point.

**Tech Stack:** pydantic-ai 1.90.0 (`[anthropic,temporal]` extras), temporalio, uv for venv management.

**Spec:** `docs/specs/2026-05-07-pydantic-ai-migration.md`

---

## File Map

### New files
- `sagaflow/engine.py` — Pydantic AI engine: `get_sdk_agent()`, `run_sdk()`, agent cache
- `tests/test_engine.py` — Unit tests for the engine layer

### Modified files
- `pyproject.toml` — Add `pydantic-ai[anthropic,temporal]` dependency
- `sagaflow/api.py` — Rewire `generate_text()` to use engine instead of helpers.spawn_with_prompt
- `sagaflow/durable/helpers.py` — Rewire `spawn_with_prompt()` and `spawn()` through engine
- `sagaflow/durable/activities.py` — Simplify `spawn_subagent` to CLI-only; SDK path removed
- `sagaflow/worker.py` — Add `PydanticAIPlugin`, add `pydantic_ai` to sandbox passthrough
- `~/code/npow-dotfiles/installers/11-sagaflow.sh` — Rewrite for uv venv

### Deleted files
- `sagaflow/transport/anthropic_sdk.py` — Replaced by Pydantic AI `AnthropicModel`
- `sagaflow/transport/dispatcher.py` — Replaced by `engine.py`
- `sagaflow/transport/structured_output.py` — SDK path uses Pydantic AI output_type; CLI path uses `_extract_json_object` already in activities.py
- `sagaflow/manifest/` — Entire directory (user confirmed not useful)
- `tests/test_anthropic_sdk.py` — Tests for deleted transport
- `tests/test_dispatcher.py` — Tests for deleted dispatcher
- `tests/test_structured_output.py` — Tests for deleted parser
- `tests/test_manifest_executor.py` — Tests for deleted manifest

### Unchanged files (verify still work)
- `sagaflow/transport/claude_cli.py`
- `sagaflow/transport/boundary.py`
- `sagaflow/transport/mcp_registry.py`
- All non-LLM activities (write_artifact, emit_finding, finalize_manifest, run_shell)
- `sagaflow/budget/`, `sagaflow/behavior.py`, `sagaflow/replay/`, `sagaflow/slack_progress.py`

---

### Task 1: Add pydantic-ai dependency and set up venv

**Files:**
- Modify: `~/code/sagaflow/pyproject.toml:33-39`
- Modify: `~/code/npow-dotfiles/installers/11-sagaflow.sh`

- [ ] **Step 1: Add pydantic-ai to pyproject.toml dependencies**

In `~/code/sagaflow/pyproject.toml`, change the `dependencies` list:

```toml
dependencies = [
    "anthropic>=0.40.0",
    "click>=8.1.0",
    "filelock>=3.12.0",
    "pydantic-ai[anthropic,temporal]>=1.80.0",
    "PyYAML>=6.0",
    "temporalio>=1.8.0",
]
```

- [ ] **Step 2: Install into sagaflow's local venv**

```bash
cd ~/code/sagaflow
uv pip install -e ".[dev]"
```

Expected: installs sagaflow + pydantic-ai + all dev deps into `~/code/sagaflow/.venv`.

- [ ] **Step 3: Verify pydantic-ai is importable**

```bash
~/code/sagaflow/.venv/bin/python -c "from pydantic_ai import Agent; from pydantic_ai.durable_exec.temporal import TemporalAgent, PydanticAIPlugin; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Rewrite the dotfiles installer**

Replace `~/code/npow-dotfiles/installers/11-sagaflow.sh` with:

```bash
#!/bin/bash
# Install sagaflow CLI into a project-local uv venv.
# Never installs into BDI system venvs (/apps/bdi-venv-*).

SAGAFLOW_REPO="$HOME/code/sagaflow"
SAGAFLOW_VENV="$SAGAFLOW_REPO/.venv"
SYMLINK="$HOME/bin/sagaflow"

if ! command -v uv >/dev/null 2>&1; then
  echo "WARN: uv not found, skipping sagaflow install."
  return 0
fi

mkdir -p "$HOME/bin"

if [ -d "$SAGAFLOW_REPO" ]; then
  # Dev machine: editable install from local checkout
  if [ ! -d "$SAGAFLOW_VENV" ]; then
    echo "Creating sagaflow venv..."
    uv venv "$SAGAFLOW_VENV" --python 3.10
  fi
  echo "Installing sagaflow (editable) into local venv..."
  uv pip install --python "$SAGAFLOW_VENV/bin/python" -e "$SAGAFLOW_REPO"
else
  # Non-dev: install from PyPI via uv tool
  echo "Installing sagaflow from PyPI..."
  uv tool install sagaflow 2>/dev/null || echo "WARN: sagaflow install via uv tool failed."
  return 0
fi

# Symlink the CLI entrypoint to ~/bin/ (already on PATH via zshrc)
SAGAFLOW_BIN="$SAGAFLOW_VENV/bin/sagaflow"
if [ -x "$SAGAFLOW_BIN" ]; then
  ln -sf "$SAGAFLOW_BIN" "$SYMLINK"
  echo "sagaflow symlinked to $SYMLINK"
else
  echo "WARN: $SAGAFLOW_BIN not found after install."
fi
```

- [ ] **Step 5: Verify ~/bin is on PATH**

```bash
grep 'HOME/bin' ~/code/npow-dotfiles/zshrc
```

Expected: line 89 contains `export PATH="$HOME/.local/bin:$HOME/bin:$PATH"`. Already present — no change needed.

- [ ] **Step 6: Commit**

```bash
cd ~/code/sagaflow
git add pyproject.toml
git commit -m "feat: add pydantic-ai[anthropic,temporal] dependency"

cd ~/code/npow-dotfiles
git add installers/11-sagaflow.sh
git commit -m "feat: rewrite sagaflow installer for uv venv, remove BDI paths"
```

---

### Task 2: Create engine.py — Pydantic AI dispatch layer

**Files:**
- Create: `sagaflow/engine.py`
- Create: `tests/test_engine.py`

- [ ] **Step 1: Write the failing test for `get_sdk_agent`**

Create `~/code/sagaflow/tests/test_engine.py`:

```python
"""Tests for sagaflow.engine — Pydantic AI dispatch layer."""

from __future__ import annotations

import pytest


def test_get_sdk_agent_returns_temporal_agent():
    from sagaflow.engine import get_sdk_agent

    agent = get_sdk_agent(
        name="test-critic",
        tier="HAIKU",
        system_prompt="You are a test agent.",
    )
    from pydantic_ai.durable_exec.temporal import TemporalAgent

    assert isinstance(agent, TemporalAgent)


def test_get_sdk_agent_caches_by_name_and_tier():
    from sagaflow.engine import get_sdk_agent, _agent_cache

    _agent_cache.clear()
    a1 = get_sdk_agent(name="cache-test", tier="HAIKU", system_prompt="x")
    a2 = get_sdk_agent(name="cache-test", tier="HAIKU", system_prompt="x")
    assert a1 is a2


def test_get_sdk_agent_different_tiers_are_different():
    from sagaflow.engine import get_sdk_agent, _agent_cache

    _agent_cache.clear()
    a1 = get_sdk_agent(name="tier-test", tier="HAIKU", system_prompt="x")
    a2 = get_sdk_agent(name="tier-test", tier="SONNET", system_prompt="x")
    assert a1 is not a2


def test_tier_to_model_mapping():
    from sagaflow.engine import TIER_TO_MODEL

    assert "HAIKU" in TIER_TO_MODEL
    assert "SONNET" in TIER_TO_MODEL
    assert "OPUS" in TIER_TO_MODEL
    assert all(v.startswith("anthropic:") for v in TIER_TO_MODEL.values())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/test_engine.py -v
```

Expected: `ModuleNotFoundError: No module named 'sagaflow.engine'`

- [ ] **Step 3: Write engine.py**

Create `~/code/sagaflow/sagaflow/engine.py`:

```python
"""Pydantic AI engine — dispatch layer replacing transport/dispatcher.

SDK path: non-tool-using LLM calls go through pydantic_ai.Agent wrapped
in TemporalAgent. Activities are auto-registered via PydanticAIPlugin.

CLI path: tool-using agents use ClaudeCliTransport (unchanged).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import ActivityConfig, TemporalAgent

logger = logging.getLogger(__name__)

TIER_TO_MODEL: dict[str, str] = {
    "HAIKU": "anthropic:claude-haiku-4-5-20251001",
    "SONNET": "anthropic:claude-sonnet-4-6",
    "OPUS": "anthropic:claude-opus-4-7",
}

_DEFAULT_TIMEOUT = timedelta(minutes=15)

_agent_cache: dict[str, TemporalAgent] = {}


def _cache_key(name: str, tier: str, system_prompt: str) -> str:
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:12]
    return f"{name}:{tier}:{prompt_hash}"


def get_sdk_agent(
    name: str,
    tier: str,
    system_prompt: str,
    output_type: type[BaseModel] | None = None,
    max_tokens: int = 128_000,
    timeout: timedelta = _DEFAULT_TIMEOUT,
) -> TemporalAgent:
    key = _cache_key(name, tier, system_prompt)
    if key in _agent_cache:
        return _agent_cache[key]

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
            start_to_close_timeout=timeout,
        ),
    )
    _agent_cache[key] = temporal_agent
    return temporal_agent


def all_cached_agents() -> list[TemporalAgent]:
    return list(_agent_cache.values())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/test_engine.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd ~/code/sagaflow
git add sagaflow/engine.py tests/test_engine.py
git commit -m "feat: add engine.py — Pydantic AI dispatch layer with agent cache"
```

---

### Task 3: Delete old transport files and their tests

**Files:**
- Delete: `sagaflow/transport/anthropic_sdk.py`
- Delete: `sagaflow/transport/dispatcher.py`
- Delete: `sagaflow/transport/structured_output.py`
- Delete: `tests/test_anthropic_sdk.py`
- Delete: `tests/test_dispatcher.py`
- Delete: `tests/test_structured_output.py`
- Delete: `sagaflow/manifest/` (entire directory)
- Delete: `tests/test_manifest_executor.py`
- Modify: `sagaflow/transport/__init__.py`

- [ ] **Step 1: Check for imports of deleted modules outside their own tests**

```bash
cd ~/code/sagaflow
grep -rn "from sagaflow.transport.anthropic_sdk import\|from sagaflow.transport.dispatcher import\|from sagaflow.transport.structured_output import" sagaflow/ --include="*.py" | grep -v __pycache__
```

Expected output shows imports in `sagaflow/durable/activities.py` — we'll fix these in Task 4. No other consumers.

```bash
grep -rn "from sagaflow.manifest" sagaflow/ --include="*.py" | grep -v __pycache__ | grep -v "run_manifest"
```

Expected: only `worker.py:309` (ManifestWorkflow import) and possibly `worker.py:417`. We'll fix worker.py in Task 5.

- [ ] **Step 2: Delete the files**

```bash
cd ~/code/sagaflow
rm sagaflow/transport/anthropic_sdk.py
rm sagaflow/transport/dispatcher.py
rm sagaflow/transport/structured_output.py
rm tests/test_anthropic_sdk.py
rm tests/test_dispatcher.py
rm tests/test_structured_output.py
rm tests/test_manifest_executor.py
rm -rf sagaflow/manifest/
```

- [ ] **Step 3: Clean up transport/__init__.py**

Read `sagaflow/transport/__init__.py`. If it imports from deleted modules, remove those imports. If it's empty or just has `__all__`, leave it.

- [ ] **Step 4: Verify remaining tests still collect (no import errors)**

```bash
cd ~/code/sagaflow
.venv/bin/pytest --collect-only 2>&1 | tail -20
```

Expected: collection errors for `test_activities.py` (imports deleted modules) — that's fine, fixed in Task 4. All other tests should collect.

- [ ] **Step 5: Commit**

```bash
cd ~/code/sagaflow
git add -A
git commit -m "refactor: delete old transport layer (anthropic_sdk, dispatcher, structured_output, manifest)"
```

---

### Task 4: Rewire spawn_subagent activity to CLI-only

**Files:**
- Modify: `sagaflow/durable/activities.py:1-25` (imports), `sagaflow/durable/activities.py:89-389` (spawn_subagent)

The `spawn_subagent` activity currently handles both SDK and CLI paths via `dispatch_subagent`. After this task, it only handles CLI (`tools_needed=True`). The SDK path is handled by Pydantic AI's auto-generated activities.

- [ ] **Step 1: Write a test for CLI-only spawn_subagent**

Add to `tests/test_activities.py` (or create if imports are too broken — check first):

```python
def test_spawn_subagent_rejects_sdk_path():
    """spawn_subagent should only handle tools_needed=True after migration."""
    from sagaflow.durable.activities import SpawnSubagentInput

    inp = SpawnSubagentInput(
        role="test",
        tier_name="HAIKU",
        system_prompt="test",
        user_prompt_path="/dev/null",
        tools_needed=False,
    )
    # SDK path should be routed through engine, not spawn_subagent
    # This is enforced by removing SDK dispatch from the activity
```

- [ ] **Step 2: Rewrite spawn_subagent imports and dispatch**

In `sagaflow/durable/activities.py`, replace the old imports (lines 17-24):

```python
# Old:
from sagaflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier
from sagaflow.transport.boundary import validate_boundary, validate_text_boundary
from sagaflow.transport.claude_cli import ClaudeCliTransport
from sagaflow.transport.dispatcher import SubagentRequest, dispatch_subagent
from sagaflow.transport.structured_output import (
    MalformedResponseError,
    parse_structured,
)
```

Replace with:

```python
from sagaflow.transport.boundary import validate_boundary, validate_text_boundary
from sagaflow.transport.claude_cli import ClaudeCliTransport
```

Remove the `_sdk_singleton`, `_get_sdk()`, `ModelTier` references.

Keep `_cli_singleton` and `_get_cli()`.

- [ ] **Step 3: Simplify spawn_subagent body**

The activity now only handles CLI path. Remove the `dispatch_subagent` call. Replace with direct `ClaudeCliTransport.call()`:

```python
@activity.defn(name="spawn_subagent")
async def spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    prompt_path = Path(inp.user_prompt_path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"subagent input file missing: {prompt_path}")
    user_prompt = prompt_path.read_text(encoding="utf-8")
    if not user_prompt.strip():
        raise FileNotFoundError(f"subagent input file is empty: {prompt_path}")

    combined_prompt = f"{inp.system_prompt}\n\n---\n\n{user_prompt}"
    run_id = Path(inp.run_dir).name if inp.run_dir else "unknown"
    label = f"{run_id}/{inp.role}:{prompt_path.stem}"

    _TIER_TO_CLI_MODEL = {"HAIKU": "haiku", "SONNET": "sonnet", "OPUS": "opus"}
    model_alias = _TIER_TO_CLI_MODEL.get(inp.tier_name, "opus")

    effective_mcp_config = inp.mcp_config_path
    if not effective_mcp_config:
        _fallback = Path.home() / ".sagaflow" / "mcp-research-minimal.json"
        if _fallback.exists():
            effective_mcp_config = str(_fallback)

    cli = _get_cli()
    beat_task: asyncio.Task[None] | None = None
    try:
        beat_task = asyncio.create_task(_heartbeat_loop())
    except RuntimeError:
        beat_task = None

    t0 = time.monotonic()
    try:
        result = await cli.call(
            prompt=combined_prompt,
            timeout_seconds=inp.cli_timeout_seconds,
            model=model_alias,
            label=label,
            dangerously_skip_permissions=True,
            mcp_config_path=effective_mcp_config,
        )
    finally:
        if beat_task is not None:
            beat_task.cancel()
            with contextlib.suppress(BaseException):
                await beat_task
    elapsed = round(time.monotonic() - t0, 1)

    raw = result.stdout
    _token_meta = {
        "_input_tokens": str(result.input_tokens),
        "_output_tokens": str(result.output_tokens),
        "_model": model_alias,
    }

    # Budget tracking (unchanged)
    _budget_enforcer = None
    if inp.run_dir:
        from sagaflow.budget.registry import get_enforcer as _get_enforcer
        try:
            _budget_enforcer = _get_enforcer(activity.info().workflow_id)
        except Exception:
            pass
        if _budget_enforcer:
            from sagaflow.cost import estimate_cost_from_result
            step_cost = estimate_cost_from_result(_token_meta)
            newly_crossed = _budget_enforcer.record_cost(step_cost)
            if newly_crossed:
                from sagaflow.budget.alerts import fire_threshold_alert
                from sagaflow.slack_progress import _read_progress_file
                routing = _read_progress_file(inp.run_dir) or {}
                for threshold in newly_crossed:
                    fire_threshold_alert(
                        threshold=threshold,
                        accumulated_cost_usd=_budget_enforcer.ledger.accumulated_cost_usd,
                        max_cost_usd=_budget_enforcer.ledger.policy.max_cost_usd or 0.0,
                        run_id=Path(inp.run_dir).name,
                        slack_channel=routing.get("channel"),
                        slack_thread_ts=routing.get("thread_ts"),
                    )

    # Run manifest tracking (unchanged)
    if inp.run_dir:
        try:
            from sagaflow.run_manifest import StepRecord, append_step as _append_step
            _append_step(
                Path(inp.run_dir),
                StepRecord(
                    step=inp.step_index,
                    role=inp.role,
                    model=model_alias,
                    tier=inp.tier_name,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    duration_seconds=elapsed,
                    status="ok",
                    output_schema_used=inp.output_schema is not None,
                ),
            )
        except Exception:
            pass

    # Replay cassette recording (unchanged)
    def _record_cassette(output: dict[str, str]) -> None:
        if not inp.run_dir:
            return
        try:
            from sagaflow.replay.cassette import hash_input, record_entry
            record_entry(
                run_dir=Path(inp.run_dir),
                run_id=Path(inp.run_dir).name,
                skill=activity.info().workflow_type or "",
                activity_name="spawn_subagent",
                role=inp.role,
                tier=inp.tier_name,
                input_hash=hash_input(inp.role, inp.system_prompt, user_prompt),
                output=output,
                duration_seconds=elapsed,
            )
        except Exception:
            logger.debug("cassette record failed", exc_info=True)

    # Parse response — CLI returns raw text, try JSON extraction
    if inp.output_schema is not None:
        import json
        parsed = _extract_json_object(raw)
        if parsed is not None:
            for k, v in list(parsed.items()):
                if not isinstance(v, str):
                    parsed[k] = json.dumps(v)
            parsed, br = validate_boundary(parsed, label=label)
            if br.truncated_fields:
                parsed["_boundary_truncated"] = ",".join(br.truncated_fields)
            parsed.update(_token_meta)
            _record_cassette(parsed)
            return parsed
        result_dict = {MALFORMED_SENTINEL: "1", "_error": "no valid JSON found", "_raw": raw[:2000], **_token_meta}
        _record_cassette(result_dict)
        return result_dict

    # Fallback: split raw text by KEY|VALUE lines
    result_dict: dict[str, str] = {"RESPONSE": raw, **_token_meta}
    _record_cassette(result_dict)
    return result_dict
```

Note: the old `parse_structured` call (STRUCTURED_OUTPUT_START/END markers) is removed. CLI agents return raw text which goes into `RESPONSE` key, or JSON if `output_schema` was set. This is simpler and matches how CLI output actually works — CLI agents don't emit structured markers.

- [ ] **Step 4: Run the existing test suite (excluding deleted test files)**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/ -v --ignore=tests/test_anthropic_sdk.py --ignore=tests/test_dispatcher.py --ignore=tests/test_structured_output.py --ignore=tests/test_manifest_executor.py -x 2>&1 | tail -30
```

Fix any import errors that surface.

- [ ] **Step 5: Commit**

```bash
cd ~/code/sagaflow
git add sagaflow/durable/activities.py
git commit -m "refactor: simplify spawn_subagent to CLI-only path"
```

---

### Task 5: Add record_sdk_telemetry activity

**Files:**
- Modify: `sagaflow/durable/activities.py` (add new activity + dataclass)
- Create: `tests/test_sdk_telemetry.py`

The SDK path through Pydantic AI bypasses the old `spawn_subagent` activity, so budget
enforcement, run manifest recording, and replay cassette recording are lost. This task
adds a lightweight `record_sdk_telemetry` activity that handles all three, called by
`spawn_with_prompt()` after `agent.run()` returns.

- [ ] **Step 1: Write the failing test**

Create `~/code/sagaflow/tests/test_sdk_telemetry.py`:

```python
"""Tests for the record_sdk_telemetry activity."""

from __future__ import annotations

import pytest
from dataclasses import asdict


def test_sdk_telemetry_input_dataclass():
    from sagaflow.durable.activities import SdkTelemetryInput

    inp = SdkTelemetryInput(
        role="critic",
        tier="HAIKU",
        system_prompt="you are a critic",
        user_prompt="review this",
        run_dir="/tmp/test-run",
        step_index=0,
        model="anthropic:claude-haiku-4-5-20251001",
        input_tokens=100,
        output_tokens=50,
        duration_seconds=1.5,
    )
    assert inp.role == "critic"
    assert inp.input_tokens == 100
    d = asdict(inp)
    assert d["tier"] == "HAIKU"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/test_sdk_telemetry.py -v
```

Expected: `ImportError: cannot import name 'SdkTelemetryInput'`

- [ ] **Step 3: Add SdkTelemetryInput dataclass and record_sdk_telemetry activity**

Add to `sagaflow/durable/activities.py`, after the `RunShellResult` class:

```python
@dataclass(frozen=True)
class SdkTelemetryInput:
    role: str
    tier: str
    system_prompt: str
    user_prompt: str
    run_dir: str
    step_index: int
    model: str
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    workflow_id: str = ""


@activity.defn(name="record_sdk_telemetry")
async def record_sdk_telemetry(inp: SdkTelemetryInput) -> str:
    """Record budget, manifest, and cassette for an SDK (Pydantic AI) call.

    Lightweight activity — no LLM, just state reads/writes. Called by
    spawn_with_prompt() after agent.run() returns.
    Returns the effective tier (may be downgraded by budget enforcement).
    """
    _token_meta = {
        "_input_tokens": str(inp.input_tokens),
        "_output_tokens": str(inp.output_tokens),
        "_model": inp.model,
    }

    # Budget: record cost
    if inp.run_dir and inp.workflow_id:
        try:
            from sagaflow.budget.registry import get_enforcer as _get_enforcer
            enforcer = _get_enforcer(inp.workflow_id)
            if enforcer:
                from sagaflow.cost import estimate_cost_from_result
                step_cost = estimate_cost_from_result(_token_meta)
                newly_crossed = enforcer.record_cost(step_cost)
                if newly_crossed:
                    from sagaflow.budget.alerts import fire_threshold_alert
                    from sagaflow.slack_progress import _read_progress_file
                    routing = _read_progress_file(inp.run_dir) or {}
                    for threshold in newly_crossed:
                        fire_threshold_alert(
                            threshold=threshold,
                            accumulated_cost_usd=enforcer.ledger.accumulated_cost_usd,
                            max_cost_usd=enforcer.ledger.policy.max_cost_usd or 0.0,
                            run_id=Path(inp.run_dir).name,
                            slack_channel=routing.get("channel"),
                            slack_thread_ts=routing.get("thread_ts"),
                        )
        except Exception:
            logger.debug("budget recording failed for SDK call", exc_info=True)

    # Run manifest: append step
    if inp.run_dir:
        try:
            from sagaflow.run_manifest import StepRecord, append_step as _append_step
            _append_step(
                Path(inp.run_dir),
                StepRecord(
                    step=inp.step_index,
                    role=inp.role,
                    model=inp.model,
                    tier=inp.tier,
                    input_tokens=inp.input_tokens,
                    output_tokens=inp.output_tokens,
                    duration_seconds=inp.duration_seconds,
                    status="ok",
                    output_schema_used=False,
                ),
            )
        except Exception:
            logger.debug("manifest recording failed for SDK call", exc_info=True)

    # Replay cassette
    if inp.run_dir:
        try:
            from sagaflow.replay.cassette import hash_input, record_entry
            record_entry(
                run_dir=Path(inp.run_dir),
                run_id=Path(inp.run_dir).name,
                skill=inp.workflow_id.split("/")[0] if inp.workflow_id else "",
                activity_name="sdk_agent",
                role=inp.role,
                tier=inp.tier,
                input_hash=hash_input(inp.role, inp.system_prompt, inp.user_prompt),
                output=_token_meta,
                duration_seconds=inp.duration_seconds,
            )
        except Exception:
            logger.debug("cassette record failed for SDK call", exc_info=True)

    return inp.tier


@dataclass(frozen=True)
class BudgetCheckInput:
    workflow_id: str
    role: str
    tier: str


@dataclass(frozen=True)
class BudgetCheckResult:
    effective_tier: str
    abort: bool
    message: str = ""


@activity.defn(name="budget_pre_dispatch")
async def budget_pre_dispatch(inp: BudgetCheckInput) -> BudgetCheckResult:
    """Check budget before an SDK dispatch. Returns effective tier (may downgrade)."""
    try:
        from sagaflow.budget.registry import get_enforcer as _get_enforcer
        enforcer = _get_enforcer(inp.workflow_id)
        if enforcer:
            resolved_tier, status = enforcer.pre_dispatch(inp.role, inp.tier)
            if status.decision.value == "abort":
                return BudgetCheckResult(
                    effective_tier=inp.tier, abort=True, message=status.message,
                )
            return BudgetCheckResult(effective_tier=resolved_tier, abort=False)
    except Exception:
        logger.debug("budget pre-dispatch check failed", exc_info=True)
    return BudgetCheckResult(effective_tier=inp.tier, abort=False)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/test_sdk_telemetry.py -v
```

Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
cd ~/code/sagaflow
git add sagaflow/durable/activities.py tests/test_sdk_telemetry.py
git commit -m "feat: add record_sdk_telemetry + budget_pre_dispatch activities"
```

---

### Task 6: Rewire helpers.py to route SDK calls through engine with telemetry

**Files:**
- Modify: `sagaflow/durable/helpers.py:1-30` (imports), `sagaflow/durable/helpers.py:165-205` (spawn_with_prompt)

- [ ] **Step 1: Write test for SDK routing**

Add to `tests/test_engine.py`:

```python
def test_spawn_with_prompt_uses_engine_for_sdk():
    """Verify spawn_with_prompt imports engine for SDK path."""
    from sagaflow.durable import helpers
    assert hasattr(helpers, "spawn_with_prompt")


def test_helpers_imports_telemetry_inputs():
    """Verify helpers can import the telemetry dataclasses."""
    from sagaflow.durable.activities import (
        SdkTelemetryInput,
        BudgetCheckInput,
        BudgetCheckResult,
    )
    assert SdkTelemetryInput is not None
    assert BudgetCheckInput is not None
    assert BudgetCheckResult is not None
```

- [ ] **Step 2: Update helpers.py imports**

Add the new dataclass imports to the existing import block in `sagaflow/durable/helpers.py`:

```python
from sagaflow.durable.activities import (
    BudgetCheckInput,
    BudgetCheckResult,
    EmitFindingInput,
    FinalizeManifestInput,
    SdkTelemetryInput,
    SpawnSubagentInput,
    WriteArtifactInput,
)
```

- [ ] **Step 3: Update spawn_with_prompt to route through engine with full telemetry**

Replace `spawn_with_prompt()` (lines 165-205):

```python
async def spawn_with_prompt(
    *,
    role: str,
    tier: str,
    system_prompt: str,
    user_prompt: str,
    run_dir: str,
    suffix: str = "",
    max_tokens: int = 128_000,
    tools_needed: bool = False,
    output_schema: dict | None = None,
    step_index: int = 0,
    mcp_config_path: str | None = None,
    cli_timeout_seconds: float = 3600.0,
    timeout: timedelta = _DEFAULT_SPAWN_TIMEOUT,
    heartbeat: timedelta | None = None,
    retry: RetryPolicy | None = None,
) -> dict[str, str]:
    """Write a prompt file then dispatch an LLM call.

    SDK path (tools_needed=False): routes through sagaflow.engine (Pydantic AI)
    with budget enforcement, cost tracking, manifest recording, and cassette.
    CLI path (tools_needed=True): writes prompt file then spawns via activity.
    """
    if not tools_needed:
        return await _dispatch_sdk(
            role=role,
            tier=tier,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            run_dir=run_dir,
            step_index=step_index,
            timeout=timeout,
        )

    # CLI path — unchanged: write prompt file, spawn via activity
    prompt_path = f"{run_dir}/{role}{suffix}-prompt.txt"
    await write(prompt_path, user_prompt)
    return await spawn(
        role=role,
        tier=tier,
        system_prompt=system_prompt,
        prompt_path=prompt_path,
        max_tokens=max_tokens,
        tools_needed=tools_needed,
        output_schema=output_schema,
        run_dir=run_dir,
        step_index=step_index,
        mcp_config_path=mcp_config_path,
        cli_timeout_seconds=cli_timeout_seconds,
        timeout=timeout,
        heartbeat=heartbeat,
        retry=retry,
    )


async def _dispatch_sdk(
    *,
    role: str,
    tier: str,
    system_prompt: str,
    user_prompt: str,
    run_dir: str,
    step_index: int = 0,
    timeout: timedelta = _DEFAULT_SPAWN_TIMEOUT,
) -> dict[str, str]:
    """SDK dispatch via Pydantic AI with budget enforcement and telemetry."""
    effective_tier = tier
    workflow_id = ""
    try:
        workflow_id = workflow.info().workflow_id
    except Exception:
        pass

    # Budget pre-dispatch: check budget, possibly downgrade tier
    if workflow_id:
        budget_result: BudgetCheckResult = await workflow.execute_activity(
            "budget_pre_dispatch",
            BudgetCheckInput(
                workflow_id=workflow_id,
                role=role,
                tier=tier,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        if budget_result.abort:
            from sagaflow.budget.enforcer import BudgetExceededError
            raise BudgetExceededError(budget_result.message)
        effective_tier = budget_result.effective_tier

    # Dispatch via Pydantic AI TemporalAgent
    with workflow.unsafe.imports_passed_through():
        from sagaflow.engine import get_sdk_agent, TIER_TO_MODEL

    agent = get_sdk_agent(
        name=role,
        tier=effective_tier,
        system_prompt=system_prompt,
        timeout=timeout,
    )
    t0 = workflow.now()
    result = await agent.run(user_prompt)
    elapsed = (workflow.now() - t0).total_seconds()

    # Extract usage from Pydantic AI result
    usage = result.usage()
    input_tokens = usage.request_tokens or 0
    output_tokens = usage.response_tokens or 0
    model = TIER_TO_MODEL.get(effective_tier, f"anthropic:{effective_tier}")

    # Record telemetry (budget cost, manifest, cassette)
    await workflow.execute_activity(
        "record_sdk_telemetry",
        SdkTelemetryInput(
            role=role,
            tier=effective_tier,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            run_dir=run_dir,
            step_index=step_index,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=elapsed,
            workflow_id=workflow_id,
        ),
        start_to_close_timeout=timedelta(seconds=15),
        retry_policy=HAIKU_POLICY,
    )

    # Build return dict with token metadata
    _token_meta = {
        "_input_tokens": str(input_tokens),
        "_output_tokens": str(output_tokens),
        "_model": model,
    }
    if isinstance(result.output, str):
        return {"RESPONSE": result.output, **_token_meta}
    if hasattr(result.output, "model_dump"):
        d = {k: str(v) for k, v in result.output.model_dump().items()}
        d.update(_token_meta)
        return d
    return {"RESPONSE": str(result.output), **_token_meta}
```

- [ ] **Step 4: Run tests**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/test_engine.py -v
```

Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd ~/code/sagaflow
git add sagaflow/durable/helpers.py
git commit -m "feat: route SDK calls through Pydantic AI engine with full telemetry"
```

---

### Task 7: Update worker.py for PydanticAIPlugin

**Files:**
- Modify: `sagaflow/worker.py:1-27` (imports, passthrough), `sagaflow/worker.py:582-655` (run_worker)

- [ ] **Step 1: Add pydantic_ai to sandbox passthrough**

In `sagaflow/worker.py`, line 27:

```python
# Old:
_PASSTHROUGH_MODULES = ("httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_", "sniffio")

# New:
_PASSTHROUGH_MODULES = ("httpx", "anthropic", "sagaflow", "pydantic", "pydantic_ai", "skills", "claude_skill_", "sniffio")
```

- [ ] **Step 2: Add PydanticAIPlugin to worker construction**

In `run_worker()`, after the worker construction (around line 637), add the plugin:

```python
# Old:
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=workflows,
        activities=all_activities,
        workflow_runner=_build_sandbox_runner(),
        max_concurrent_activities=_max_activities,
        max_concurrent_workflow_tasks=_max_wf_tasks,
        debug_mode=True,
    )

# New:
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=workflows,
        activities=all_activities,
        plugins=[PydanticAIPlugin()],
        workflow_runner=_build_sandbox_runner(),
        max_concurrent_activities=_max_activities,
        max_concurrent_workflow_tasks=_max_wf_tasks,
        debug_mode=True,
    )
```

- [ ] **Step 3: Register new telemetry activities in worker**

In `run_worker()`, where `combined` activities are assembled (around line 607), add the new activities:

```python
from sagaflow.durable.activities import (
    record_sdk_telemetry as _record_sdk_act,
    budget_pre_dispatch as _budget_pre_act,
)
combined.extend([_record_sdk_act, _budget_pre_act])
```

Add this alongside the existing `combined.extend(...)` calls for slack, finalize, run_shell, etc.

- [ ] **Step 4: Remove ManifestWorkflow references from worker.py**

In `_register_manifested_skills()` (line 297-358) and `build_extra_workflows()` (line 415-419): remove the ManifestWorkflow imports and registration since the manifest directory is deleted. Replace with pass/skip.

In `build_extra_workflows()`, remove the ManifestWorkflow block:

```python
# Delete this block (lines 416-419):
    try:
        from sagaflow.manifest.temporal import ManifestWorkflow
        extras.append(ManifestWorkflow)
    except ImportError:
        pass
```

In `_register_manifested_skills()`, gut the function:

```python
def _register_manifested_skills(registry: SkillRegistry, skills_root: "Path") -> None:
    """Manifest-based skill registration — disabled pending v2 migration."""
    pass
```

- [ ] **Step 5: Run worker import check**

```bash
cd ~/code/sagaflow
.venv/bin/python -c "from sagaflow.worker import run_worker; print('OK')"
```

Expected: `OK`

- [ ] **Step 6: Run full test suite**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/ -v -x 2>&1 | tail -40
```

Fix any remaining import errors. Expected: most tests pass. Some skill-specific tests may need adjustments if they import deleted modules.

- [ ] **Step 7: Commit**

```bash
cd ~/code/sagaflow
git add sagaflow/worker.py
git commit -m "feat: add PydanticAIPlugin + telemetry activities to worker, remove ManifestWorkflow"
```

---

### Task 8: Update api.py generate_text() to use engine

**Files:**
- Modify: `sagaflow/api.py:192-251` (generate_text)

- [ ] **Step 1: Simplify generate_text() SDK path**

The current `generate_text()` calls `helpers.spawn_with_prompt()` which now routes through the engine. The change here is minimal — just verify the import chain works. However, we should also update `generate_text()` to not pass `tools_needed` through by default (it's already False by default in the prompt config).

No code change needed — `generate_text()` calls `spawn_with_prompt()` which already routes through the engine after Task 5. Verify:

```bash
cd ~/code/sagaflow
.venv/bin/python -c "
from sagaflow.api import generate_text
print('generate_text imported OK')
"
```

Expected: `generate_text imported OK`

- [ ] **Step 2: Run full test suite one more time**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/ -v 2>&1 | tail -40
```

Expected: all tests pass (excluding deleted test files).

- [ ] **Step 3: Commit if any api.py changes were needed**

```bash
cd ~/code/sagaflow
git diff --stat
# Only commit if there are changes
```

---

### Task 9: Bump version and final verification

**Files:**
- Modify: `sagaflow/pyproject.toml:7` (version)

- [ ] **Step 1: Bump version**

In `pyproject.toml`, change version from `0.9.12` to `0.10.0` (minor bump for the Pydantic AI migration):

```toml
version = "0.10.0"
```

- [ ] **Step 2: Run the full test suite**

```bash
cd ~/code/sagaflow
.venv/bin/pytest tests/ -v --tb=short 2>&1
```

Expected: all tests pass.

- [ ] **Step 3: Verify imports**

```bash
cd ~/code/sagaflow
.venv/bin/python -c "
from sagaflow import workflow, generate_text, parallel, write_file, progress
from sagaflow.engine import get_sdk_agent, TIER_TO_MODEL, all_cached_agents
from sagaflow.durable.helpers import spawn_with_prompt, spawn_parallel, finalize
from sagaflow.durable.activities import spawn_subagent, write_artifact, emit_finding, record_sdk_telemetry, budget_pre_dispatch
from sagaflow.worker import run_worker, build_registry
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalAgent
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 4: Verify deleted modules are gone**

```bash
cd ~/code/sagaflow
.venv/bin/python -c "
import sys
for mod in ['sagaflow.transport.anthropic_sdk', 'sagaflow.transport.dispatcher', 'sagaflow.transport.structured_output']:
    try:
        __import__(mod)
        print(f'FAIL: {mod} still importable')
        sys.exit(1)
    except ImportError:
        print(f'OK: {mod} deleted')
print('All deleted modules confirmed gone')
"
```

Expected: all three report `OK: ... deleted`

- [ ] **Step 5: Commit**

```bash
cd ~/code/sagaflow
git add pyproject.toml
git commit -m "chore: bump version to 0.10.0 for Pydantic AI migration"
```

- [ ] **Step 6: Summary of all commits**

```bash
cd ~/code/sagaflow
git log --oneline -8
```

Expected: 8 commits from this plan (Tasks 1-9) on top of the spec commit.
