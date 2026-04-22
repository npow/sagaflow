# skillflow

Temporal-backed workflow runtime for Claude Code skills. Spawn durable subagent orchestrations that survive session crashes, surface results through multiple layers, and retry on failure.

## Install

```bash
pip install skillflow
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
skillflow doctor
skillflow launch hello-world --name alice --await
```

You should see `hello, alice` printed, an entry appended to `~/.skillflow/INBOX.md`, and a desktop notification.

## Architecture

- `skillflow launch <skill>` submits a workflow to a local Temporal server.
- A long-lived worker daemon (auto-spawned) polls the `skillflow` task queue and runs the workflow.
- Subagent activities dispatch to either the Anthropic SDK (default) or `claude -p` subprocess (when tools are needed).
- Results land in `~/.skillflow/INBOX.md`, trigger a desktop notification, and are available via `skillflow show <run_id>`.
- A Claude Code SessionStart hook (auto-installed) surfaces unread entries to new Claude Code sessions.

## Writing a new skill

See `docs/SKILL-TEMPLATE.md`.

## CLI reference

- `skillflow launch <skill> [...] [--await]` — submit workflow
- `skillflow list` — show recent runs
- `skillflow show <run_id>` — dump report
- `skillflow inbox` — show unread INBOX entries
- `skillflow dismiss <run_id>` — mark as read
- `skillflow worker run` — foreground worker (use only for debugging)
- `skillflow hook install|uninstall|session-start` — hook lifecycle (auto-installed on first launch)
- `skillflow doctor` — preflight checks

## License

MIT
