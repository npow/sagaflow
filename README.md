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

## Authoring a workflow

```python
from sagaflow import workflow, generate_text, parallel, write_file

@workflow(name="code-review", phases=["Critique", "Synthesize"])
async def run(diff: str):
    findings = await parallel(
        generate_text("security-critic", variables={"diff": diff}),
        generate_text("perf-critic", variables={"diff": diff}),
    )
    report = await generate_text("synth", variables={"findings": str(findings)})
    await write_file("report.md", report)
    return report
```

Prompts live in `prompts/<name>.prompt` next to the workflow file. Each `generate_text` and `write_file` call is a Temporal activity with its own retry policy and replay log.

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
