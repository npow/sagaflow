# sagaflow

[![CI](https://github.com/npow/sagaflow/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/sagaflow/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sagaflow.svg)](https://pypi.org/project/sagaflow/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-mintlify-18a34a?style=flat-square)](https://mintlify.com/npow/sagaflow)

Run durable agent workflows that outlive your session.

## The problem

Multi-agent skills for code review, debugging, and research spawn parallel subagents and thread their output back together through ad-hoc file-based state machines. When the session crashes — or a subagent wedges silently for hours — that state fragments, retries are brittle prose inside markdown, and there's no visibility into what's still in flight. Rolling a durable execution layer per skill duplicates a lot of work that [Temporal](https://temporal.io/) already solves.

## Quick start

```bash
pip install sagaflow
temporal server start-dev &
export ANTHROPIC_API_KEY=sk-ant-...
sagaflow launch hello-world --name alice --await
# → hello, alice
```

A `DONE` entry also lands in `~/.sagaflow/INBOX.md` and fires a desktop notification. Kill your terminal mid-run and re-launch: the workflow resumes from the last completed activity.

## Install

```bash
pip install sagaflow
```

Requirements:
- Python 3.11+
- [Temporal CLI](https://docs.temporal.io/cli) running locally: `brew install temporal && temporal server start-dev`
- An Anthropic API key: `export ANTHROPIC_API_KEY=sk-ant-...`

Optional: set `ANTHROPIC_BASE_URL` to route through any Anthropic-compatible proxy (Bedrock, a local model gateway, etc.).

## Usage

### Launch and wait for the result

```bash
sagaflow launch hello-world --name alice --await
```

### Fire and forget; check the inbox later

```bash
sagaflow launch hello-world --name alice
sagaflow inbox
# [2026-04-22 14:33:22] hello-world-20260422-143322 DONE hello-world  hello, alice
sagaflow dismiss hello-world-20260422-143322
```

### Diagnose a broken setup

```bash
sagaflow doctor
# [OK] temporal
# [OK] transport
# [WARN] worker: no worker polling; will auto-spawn on launch
# [OK] hook
```

## How it works

```
sagaflow launch <skill> --await
        │
        ▼
preflight → auto-install SessionStart hook
         → auto-spawn worker daemon if none running
         → submit workflow to Temporal (localhost:7233)
         │
         ▼
worker daemon polls task queue "sagaflow"
         runs @workflow.defn → executes activities:
           • write_artifact     (file I/O)
           • spawn_subagent     (Anthropic SDK or `claude -p`)
           • emit_finding       (INBOX + desktop notify)
         │
         ▼
4-layer result-surfacing safety net:
  1. --await completion → caller prints
  2. ~/.sagaflow/INBOX.md (append-only)
  3. SessionStart hook → next Claude Code session surfaces unread
  4. desktop notification (osascript / notify-send)
```

If the worker crashes mid-run, the next `sagaflow launch` auto-spawns a fresh one and Temporal resumes from the last completed activity.

## Writing a new skill

See [`docs/SKILL-TEMPLATE.md`](docs/SKILL-TEMPLATE.md). The minimal skill is `skills/hello_world/` (~100 lines), which exercises every framework surface without importing anything skill-specific from the framework core.

## Development

```bash
git clone https://github.com/npow/sagaflow
cd sagaflow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check sagaflow tests skills
mypy sagaflow
pytest

# Opt-in end-to-end tests (require live Temporal + real Anthropic access)
SAGAFLOW_E2E=1 pytest
```

## License

[MIT](LICENSE)
