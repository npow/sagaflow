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
    click.echo("(not yet implemented — see Task 17)")


@main.command()
@click.argument("run_id")
def show(run_id: str) -> None:
    """Dump the final report for a run."""
    click.echo(f"(not yet implemented — would show {run_id})")


@main.command()
def inbox() -> None:
    """List unread INBOX entries."""
    click.echo("(not yet implemented — see Task 17)")


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
