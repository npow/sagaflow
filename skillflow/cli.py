"""skillflow CLI entry point."""

from __future__ import annotations

import asyncio
import sys

import click


@click.group()
def main() -> None:
    """skillflow — Temporal-backed workflow runtime for Claude Code skills."""


# Stubs — CLI subcommands call these; tests patch them.
def _preflight_all() -> None: ...
def _ensure_hook_installed() -> None: ...
def _ensure_worker_running() -> None: ...
def _start_workflow(skill: str, args: dict) -> str: ...  # type: ignore[type-arg]
def _await_workflow(workflow_id: str) -> str: ...


# --- internals used by subcommands; patched in tests ---
def _inbox():  # type: ignore[no-untyped-def]
    from skillflow.inbox import Inbox
    from skillflow.paths import Paths
    return Inbox(path=Paths.from_env().inbox)


def _list_workflows() -> list[dict]:  # type: ignore[type-arg]
    # Placeholder until Task 24 wires Temporal; return empty list.
    return []


@main.command()
@click.argument("skill")
@click.option("--name", default="world", help="hello-world: greeting target name")
@click.option("--await", "await_result", is_flag=True, help="Block until the workflow finishes")
def launch(skill: str, name: str, await_result: bool) -> None:
    """Launch a skill workflow. Non-blocking by default; --await blocks on result."""

    _preflight_all()
    _ensure_hook_installed()
    _ensure_worker_running()
    args = {"name": name}  # skill-specific; hello-world takes name
    workflow_id = _start_workflow(skill, args)
    click.echo(f"Launched {workflow_id}")
    if await_result:
        result = _await_workflow(workflow_id)
        click.echo(result)


@main.command(name="list")
def list_cmd() -> None:
    """List running, completed, and failed runs."""
    rows = _list_workflows()
    if not rows:
        click.echo("no workflows to list")
        return
    for row in rows:
        click.echo(f"{row['id']} {row['status']}")


@main.command()
def inbox() -> None:
    """List unread INBOX entries."""
    entries = _inbox().unread()
    if not entries:
        click.echo("no unread entries")
        return
    for e in entries:
        ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        click.echo(f"[{ts}] {e.run_id} {e.status} {e.skill}  {e.summary}")


@main.command()
@click.argument("run_id")
def dismiss(run_id: str) -> None:
    """Dismiss (mark read) an INBOX entry by run ID."""
    _inbox().dismiss(run_id)
    click.echo(f"dismissed {run_id}")


@main.command()
@click.argument("run_id")
def show(run_id: str) -> None:
    """Dump the final report for a run."""
    from skillflow.paths import Paths
    report = Paths.from_env().run_dir_for(run_id) / "report.md"
    if not report.exists():
        click.echo(f"no report at {report}")
        return
    click.echo(report.read_text())


@main.group()
def hook() -> None:
    """Hook management (install / uninstall / session-start)."""


@main.command()
def doctor() -> None:
    """Run preflight checks (Temporal, transport, worker, hook)."""
    click.echo("(not yet implemented — see Task 19)")


@main.group()
def worker() -> None:
    """Worker daemon lifecycle."""


if __name__ == "__main__":
    main()
