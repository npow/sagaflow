# sagaflow

[![CI](https://github.com/npow/sagaflow/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/sagaflow/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sagaflow.svg)](https://pypi.org/project/sagaflow/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Durable execution for long-running agent workflows, on top of [Temporal](https://temporal.io/).

You write a Python workflow that calls models, runs tools, and writes artifacts. sagaflow runs each step as a Temporal activity, so when the worker dies — or a 40-minute fan-out crashes halfway — the next launch resumes from the last completed step instead of starting over. Results land in `~/.sagaflow/INBOX.md` whether or not you're still attached to the session that started them.

## Quick start

```bash
pip install sagaflow
temporal server start-dev &
export ANTHROPIC_API_KEY=sk-ant-...

sagaflow launch hello-world --name alice --await
# → hello, alice
```

Kill the terminal mid-run and re-launch the same workflow ID: it picks up from where the worker died.

## What you get

- **Resumes after crashes.** Activity-level checkpointing via Temporal — workers, sessions, and laptops can all die without losing in-flight work.
- **Decoupled from the caller.** Fire-and-forget submissions land in an append-only inbox; a session-start hook surfaces unread results next time you open Claude Code.
- **Provider-agnostic transport.** Anthropic SDK by default; point `ANTHROPIC_BASE_URL` at Bedrock, a model gateway, or any compatible proxy.
- **Auto-managed worker.** First `sagaflow launch` spawns a worker daemon; `sagaflow doctor` reports health.

## Install

```bash
pip install sagaflow
```

Requirements:
- Python 3.11+
- [Temporal CLI](https://docs.temporal.io/cli) running locally: `brew install temporal && temporal server start-dev`
- An Anthropic API key (or a compatible proxy via `ANTHROPIC_BASE_URL`)

## Authoring a skill

A skill is a directory under `~/.claude/skills/<skill-name>/` containing three things:

1. **`workflow.py`** — a Temporal workflow class (the durable orchestration)
2. **`__init__.py`** — a `register()` function that wires the workflow into sagaflow
3. **`prompts/*.md`** — the system/user prompts the workflow's activities load

Here is the complete `hello-world` skill (the one `sagaflow launch hello-world --name alice` runs).

**`~/.claude/skills/hello-world/workflow.py`** — the durable workflow:

```python
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.durable.activities import (
        EmitFindingInput, SpawnSubagentInput, WriteArtifactInput,
    )
    from sagaflow.durable.retry_policies import HAIKU_POLICY


@dataclass(frozen=True)
class HelloWorldInput:
    run_id: str
    name: str
    inbox_path: str
    run_dir: str
    greeter_system_prompt: str
    greeter_user_prompt: str


@workflow.defn(name="HelloWorldWorkflow")
class HelloWorldWorkflow:
    @workflow.run
    async def run(self, inp: HelloWorldInput) -> str:
        prompt_path = f"{inp.run_dir}/prompt.txt"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=prompt_path, content=inp.greeter_user_prompt),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        parsed = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="greeter", tier_name="HAIKU",
                system_prompt=inp.greeter_system_prompt,
                user_prompt_path=prompt_path,
                max_tokens=64, tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=600),
            heartbeat_timeout=timedelta(seconds=120),
            retry_policy=HAIKU_POLICY,
        )
        greeting = parsed.get("GREETING", "hello")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path, run_id=inp.run_id,
                skill="hello-world", status="DONE", summary=greeting,
                timestamp_iso=workflow.now().isoformat(timespec="seconds"),
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return greeting
```

Each `execute_activity` call is a checkpoint. If the worker dies between them, replay resumes from the last completed one.

**`~/.claude/skills/hello-world/__init__.py`** — registration:

```python
from typing import Any

from sagaflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from sagaflow.prompts import load_prompt
from sagaflow.registry import SkillRegistry, SkillSpec

from skills.hello_world.workflow import HelloWorldInput, HelloWorldWorkflow


def _build_input(*, run_id, run_dir, inbox_path, cli_args: dict[str, Any]) -> HelloWorldInput:
    name = str(cli_args.get("name", "world"))
    return HelloWorldInput(
        run_id=run_id, name=name,
        inbox_path=inbox_path, run_dir=run_dir,
        greeter_system_prompt=load_prompt(__file__, "greeter.system"),
        greeter_user_prompt=load_prompt(__file__, "greeter.user", substitutions={"name": name}),
    )


def register(registry: SkillRegistry) -> None:
    registry.register(SkillSpec(
        name="hello-world",
        workflow_cls=HelloWorldWorkflow,
        activities=[write_artifact, emit_finding, spawn_subagent],
        build_input=_build_input,
    ))
```

`register()` is what the worker calls at startup to discover the skill. `_build_input` translates CLI args (`--name alice`) into the workflow's input dataclass and loads prompts from disk.

**`~/.claude/skills/hello-world/prompts/greeter.system.md`**:

```
You are a greeter. Output a greeting using the format
STRUCTURED_OUTPUT_START / GREETING|<text> / STRUCTURED_OUTPUT_END.
Do not include any other text.
```

**`~/.claude/skills/hello-world/prompts/greeter.user.md`**:

```
Greet $name
```

That's the whole skill. `sagaflow launch hello-world --name alice` finds the registration, builds the input, hands the workflow to Temporal, and the worker runs it durably.

## CLI

```bash
sagaflow launch <name> --arg key=value [--await]   # submit a workflow
sagaflow inbox                                     # list unread results
sagaflow dismiss <run-id>                          # mark as read
sagaflow doctor                                    # diagnose temporal/worker/hook
```

## How it works

```
sagaflow launch
   │
   ▼
preflight (install hook, spawn worker if missing)
   │
   ▼
Temporal (localhost:7233) ── workflow ID ── worker daemon
                                              │
                                              ▼
                                         activities:
                                          • model calls
                                          • file I/O
                                          • inbox emit
                                              │
                                              ▼
                              ~/.sagaflow/INBOX.md  +  desktop notify
                                              │
                                              ▼
                                  next session: SessionStart
                                  hook surfaces unread runs
```

If the worker crashes mid-run, the next `sagaflow launch` (or the next worker poll) resumes from the last completed activity. Activities that already succeeded don't re-execute.

## Development

```bash
git clone https://github.com/npow/sagaflow
cd sagaflow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check sagaflow tests
mypy sagaflow
pytest

# Opt-in end-to-end tests (require live Temporal + real Anthropic access)
SAGAFLOW_E2E=1 pytest
```

## License

[MIT](LICENSE)
