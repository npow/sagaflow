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
    """Hook management (install / uninstall / session-start reader)."""


@hook.command(name="install")
def hook_install() -> None:
    from skillflow.hook import install
    install()
    click.echo("hook installed")


@hook.command(name="uninstall")
def hook_uninstall() -> None:
    from skillflow.hook import uninstall
    uninstall()
    click.echo("hook uninstalled")


@hook.command(name="session-start")
def hook_session_start() -> None:
    from skillflow.hook import format_session_start_context
    click.echo(format_session_start_context(inbox=_inbox()), nl=False)


def _ensure_hook_installed() -> None:
    from skillflow.hook import install, is_installed
    if not is_installed():
        install()


# --- doctor probes ---


def _probe_temporal() -> tuple[str, str | None]:
    import asyncio as _a
    from skillflow.temporal_client import TemporalUnreachable, preflight
    try:
        _a.run(preflight())
        return ("OK", None)
    except TemporalUnreachable as exc:
        return ("FAIL", str(exc))


def _probe_transport() -> tuple[str, str | None]:
    import asyncio as _a
    from skillflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier
    try:
        async def _call() -> None:
            await AnthropicSdkTransport().call(
                tier=ModelTier.HAIKU,
                system_prompt="ping",
                user_prompt="ping",
                max_tokens=8,
            )
        _a.run(_call())
        return ("OK", None)
    except Exception as exc:  # noqa: BLE001
        return ("FAIL", str(exc))


def _probe_worker() -> tuple[str, str | None]:
    import asyncio as _a
    from skillflow.temporal_client import connect
    from skillflow.worker import _is_worker_reachable
    try:
        async def _go() -> bool:
            client = await connect()
            return await _is_worker_reachable(client)
        running = _a.run(_go())
        return ("OK", None) if running else ("WARN", "no worker polling; will auto-spawn on launch")
    except Exception as exc:  # noqa: BLE001
        return ("FAIL", str(exc))


def _probe_hook() -> tuple[str, str | None]:
    from skillflow.hook import is_installed
    return ("OK", None) if is_installed() else ("WARN", "hook not installed; auto-installs on first launch")


@main.command()
def doctor() -> None:
    """Run preflight checks."""
    checks = [
        ("temporal", _probe_temporal),
        ("transport", _probe_transport),
        ("worker", _probe_worker),
        ("hook", _probe_hook),
    ]
    any_fail = False
    for label, probe in checks:
        status, detail = probe()
        msg = f"[{status}] {label}"
        if detail:
            msg += f": {detail}"
        click.echo(msg)
        if status == "FAIL":
            any_fail = True
    if any_fail:
        sys.exit(1)


@main.group()
def worker() -> None:
    """Worker daemon lifecycle."""


@worker.command(name="run")
@click.option("--detached-child", is_flag=True, hidden=True)
def worker_run(detached_child: bool) -> None:
    """Foreground worker. Blocks until killed."""

    import asyncio as _asyncio

    from skillflow.worker import run_worker

    _asyncio.run(run_worker())


def _ensure_worker_running() -> None:
    import asyncio as _asyncio

    from skillflow.worker import ensure_worker_running

    _asyncio.run(ensure_worker_running())


if __name__ == "__main__":
    main()
