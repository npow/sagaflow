# sagaflow

[![CI](https://github.com/npow/sagaflow/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/sagaflow/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sagaflow.svg)](https://pypi.org/project/sagaflow/)
[![Python](https://img.shields.io/pypi/pyversions/sagaflow.svg)](https://pypi.org/project/sagaflow/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Temporal-backed workflow runtime for Claude Code skills.

Ship subagent orchestrations that **survive session crashes, retry on failure, and surface results through multiple independent layers** — without writing your own durability layer. You define a `@workflow.defn` class that executes activities; Temporal handles the rest.

> **Status: pre-alpha.** APIs may change. Pinned for development use alongside the `hello-world` smoke skill. Feedback welcome via [issues](https://github.com/npow/sagaflow/issues).

## Why

Coordination skills inside Claude Code (`deep-qa`, `deep-debug`, etc.) spawn parallel subagents and synchronise through a file-based state machine. That model has real weaknesses:

- No cross-session durability. Claude Code dies mid-run → orchestration fragments.
- PID-liveness ≠ progress. A subagent can wedge silently for hours.
- Retries are ad-hoc prose in each skill's SKILL.md, inconsistent under LLM drift.
- No cross-skill visibility. Grepping run directories to find in-flight work.

sagaflow moves coordination into Python [Temporal](https://temporal.io/) workflows. Skill activities become durable, observable units; Temporal handles retries, timeouts, and replay-from-last-completed-activity on worker restart.

## Install

```bash
pip install sagaflow
```

Requirements:
- Python 3.11+
- [Temporal CLI](https://docs.temporal.io/cli) running locally: `brew install temporal && temporal server start-dev`
- Anthropic API key: `export ANTHROPIC_API_KEY=sk-ant-...`

Optional: set `ANTHROPIC_BASE_URL` to route through any OpenAI/Anthropic-compatible proxy (Bedrock, a local model gateway, etc.).

## Quick start

```bash
sagaflow doctor
sagaflow launch hello-world --name alice --await
```

Expected: `hello, alice` printed, an entry appended to `~/.sagaflow/INBOX.md`, and a desktop notification.

## How it works

```
user types:  sagaflow launch <skill> --await
                    │
                    ▼
┌───────────────────────────────────────────────┐
│ preflight → auto-install SessionStart hook    │
│ → auto-spawn worker daemon if none running    │
│ → submit workflow to Temporal (localhost:7233)│
└───────────────────────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────┐
│ worker daemon polls task queue ("sagaflow")   │
│ runs @workflow.defn → executes activities:    │
│   - write_artifact (file I/O)                 │
│   - spawn_subagent (Anthropic SDK or claude -p)│
│   - emit_finding   (INBOX + notify)           │
└───────────────────────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────┐
│ 4-layer result-surfacing safety net:          │
│   1. --await completion → caller prints       │
│   2. ~/.sagaflow/INBOX.md (append-only)       │
│   3. SessionStart hook → next Claude session  │
│   4. desktop notification (osascript/notify-send)│
└───────────────────────────────────────────────┘
```

If the worker crashes mid-run, the next `sagaflow launch` auto-spawns a fresh one and Temporal resumes from the last completed activity.

## CLI reference

| Command | Purpose |
|---|---|
| `sagaflow launch <skill> [...] [--await]` | submit workflow; `--await` blocks on completion |
| `sagaflow list` | list recent runs |
| `sagaflow show <run_id>` | dump the final report |
| `sagaflow inbox` | show unread INBOX entries |
| `sagaflow dismiss <run_id>` | mark as read |
| `sagaflow worker run` | foreground worker (debugging) |
| `sagaflow hook install\|uninstall\|session-start` | hook lifecycle (auto-installed on first launch) |
| `sagaflow doctor` | preflight checks: Temporal, transport, worker, hook |

## Writing a new skill

See [`docs/SKILL-TEMPLATE.md`](docs/SKILL-TEMPLATE.md). The minimal-viable-skill is `skills/hello_world/` (~100 lines) which exercises every framework surface.

## Development

```bash
git clone https://github.com/npow/sagaflow
cd sagaflow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Lint, type-check, test
ruff check sagaflow tests skills
mypy sagaflow
pytest

# Include the opt-in end-to-end tests (requires live Temporal + Anthropic access)
SAGAFLOW_E2E=1 pytest
```

## Publishing

Releases publish to PyPI via [GitHub Actions Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — no API tokens stored anywhere. To cut a release:

1. Bump `version` in `pyproject.toml`.
2. Create a GitHub release with a matching tag (e.g. `v0.1.1`).
3. The `Publish to PyPI` workflow in `.github/workflows/publish.yml` runs automatically and uploads the sdist + wheel.

Before the first release, register the workflow as a Trusted Publisher at <https://pypi.org/manage/account/publishing/> with:
- Project name: `sagaflow`
- Owner: `npow`, Repository: `sagaflow`
- Workflow: `publish.yml`, Environment: `pypi`

## License

MIT — see [LICENSE](LICENSE).
