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

## Generic interpreter

Any claude-skill with a `SKILL.md` is automatically Temporal-runnable — no Python needed:

```bash
sagaflow launch my-new-skill --arg topic='something' --await
```

sagaflow reads the SKILL.md, runs Claude in a tool-use loop where each tool call is a distinct Temporal activity, and writes the result to `~/.sagaflow/runs/<id>/`. If the worker crashes mid-run, Temporal resumes from the last completed activity.

For skills that benefit from explicit parallelism (fan-out critics, independent judges), add a `workflow.py` alongside the SKILL.md in claude-skills. sagaflow discovers it at runtime.

## Mission mode (ex-swarmd)

Run a claude agent against success criteria with tamper detection, anti-cheat, and progress monitoring:

```bash
sagaflow mission launch mission.yaml
sagaflow mission status <workflow-id>
sagaflow mission abort <workflow-id>
```

See `docs/specs/` for mission.yaml format and observer details.

## Skills

Skills live in the [claude-skills](https://github.com/npow/claude-skills) repo (`~/.claude/skills/`). sagaflow discovers them dynamically at worker startup — zero skill-specific imports in sagaflow. 11 skills have bespoke Temporal workflows:

| Skill | What it does |
|---|---|
| `hello-world` | Framework smoke test — greets a name and emits a finding |
| `deep-qa` | Multi-round QA of docs, code, research, or skills with parallel critics and synthesis |
| `deep-debug` | Hypothesis-driven debugging: generate → judge → synthesize root-cause report |
| `deep-research` | WHO/WHAT/HOW/WHERE/WHEN/WHY dimension expansion with per-direction findings |
| `deep-design` | Draft spec → critique × N → redesign → final spec.md |
| `deep-plan` | Planner → Architect → Critic consensus loop with ADR output |
| `proposal-reviewer` | Claim extraction + 4-dimension critique + fact-check + assembly |
| `team` | Plan → PRD → N parallel workers → verify → fix loop |
| `autopilot` | Expand → plan → exec → qa → validate (3 judges) → completion report |
| `loop-until-done` | PRD + falsifiability judge + per-criterion verify loop until all pass |
| `flaky-test-diagnoser` | Multi-run N × → hypothesis generation → judge → report |

All skills use the same transport layer (Anthropic SDK or `claude -p` subprocess) and the same 4-layer result-surfacing (INBOX → SessionStart hook → desktop notify → `--await` return).

## Writing a new skill

**Simple skill (no Python):** Add `~/.claude/skills/my-skill/SKILL.md`. Done — the generic interpreter handles it.

**Bespoke skill (with parallelism):** Add `__init__.py` + `workflow.py` alongside SKILL.md. The workflow imports sagaflow types (`SpawnSubagentInput`, `WriteArtifactInput`, retry policies) and is discovered dynamically. See `~/.claude/skills/hello-world-temporal/` for the minimal example.

## Development

```bash
git clone https://github.com/npow/sagaflow
git clone https://github.com/npow/claude-skills ~/.claude/skills  # or your existing checkout
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
