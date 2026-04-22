# sagaflow

Temporal-backed workflow runtime for Claude Code skills. Spawn durable subagent orchestrations that survive session crashes, surface results through multiple layers, and retry on failure.

## Install

```bash
pip install sagaflow
# or from source:
pip install -e .
```

Requirements:
- Python 3.11+
- [Temporal CLI](https://docs.temporal.io/cli) running locally: `brew install temporal && temporal server start-dev`
- Anthropic API key: `export ANTHROPIC_API_KEY=sk-ant-...`

Optional: set `ANTHROPIC_BASE_URL` to route through a compatible proxy (e.g., Bedrock, model gateway).

## Quick start

```bash
sagaflow doctor
sagaflow launch hello-world --name alice --await
```

You should see `hello, alice` printed, an entry appended to `~/.sagaflow/INBOX.md`, and a desktop notification.

## Architecture

- `sagaflow launch <skill>` submits a workflow to a local Temporal server.
- A long-lived worker daemon (auto-spawned) polls the `sagaflow` task queue and runs the workflow.
- Subagent activities dispatch to either the Anthropic SDK (default) or `claude -p` subprocess (when tools are needed).
- Results land in `~/.sagaflow/INBOX.md`, trigger a desktop notification, and are available via `sagaflow show <run_id>`.
- A Claude Code SessionStart hook (auto-installed) surfaces unread entries to new Claude Code sessions.

## Writing a new skill

See `docs/SKILL-TEMPLATE.md`.

## CLI reference

- `sagaflow launch <skill> [...] [--await]` — submit workflow
- `sagaflow list` — show recent runs
- `sagaflow show <run_id>` — dump report
- `sagaflow inbox` — show unread INBOX entries
- `sagaflow dismiss <run_id>` — mark as read
- `sagaflow worker run` — foreground worker (use only for debugging)
- `sagaflow hook install|uninstall|session-start` — hook lifecycle (auto-installed on first launch)
- `sagaflow doctor` — preflight checks

## License

MIT
