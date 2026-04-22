"""sagaflow CLI entry point."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, cast

import click

if TYPE_CHECKING:
    from sagaflow.inbox import Inbox


@click.group()
def main() -> None:
    """sagaflow — Temporal-backed workflow runtime for Claude Code skills."""


# Stubs — CLI subcommands call these; tests patch them.
def _preflight_all() -> None:
    import asyncio as _a
    from sagaflow.temporal_client import preflight

    _a.run(preflight())


def _start_workflow(skill: str, args: dict) -> str:  # type: ignore[type-arg]
    import asyncio as _a
    from datetime import datetime
    from sagaflow.temporal_client import TASK_QUEUE, connect
    from sagaflow.worker import build_registry

    async def _go() -> str:
        client = await connect()
        registry = build_registry()
        spec = registry.get(skill)
        run_id = f"{skill}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        from sagaflow.paths import Paths

        paths = Paths.from_env()
        paths.ensure()
        run_dir = paths.run_dir_for(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # For hello-world specifically:
        if skill == "hello-world":
            from skills.hello_world.workflow import HelloWorldInput

            wf_input = HelloWorldInput(
                run_id=run_id,
                name=args["name"],
                inbox_path=str(paths.inbox),
                run_dir=str(run_dir),
            )
            handle = await client.start_workflow(
                spec.workflow_cls.run,
                wf_input,
                id=run_id,
                task_queue=TASK_QUEUE,
            )
            return handle.id
        raise NotImplementedError(f"launch wiring missing for skill {skill!r}")

    return _a.run(_go())


def _await_workflow(workflow_id: str) -> str:
    import asyncio as _a
    from sagaflow.temporal_client import connect

    async def _go() -> str:
        client = await connect()
        handle = client.get_workflow_handle(workflow_id)
        return cast(str, await handle.result())

    return _a.run(_go())


# --- internals used by subcommands; patched in tests ---
def _inbox() -> "Inbox":
    from sagaflow.inbox import Inbox
    from sagaflow.paths import Paths
    return Inbox(path=Paths.from_env().inbox)


def _list_workflows() -> list[dict[str, str]]:
    # Placeholder until a later task wires Temporal workflow listing.
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
    from sagaflow.paths import Paths
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
    from sagaflow.hook import install
    install()
    click.echo("hook installed")


@hook.command(name="uninstall")
def hook_uninstall() -> None:
    from sagaflow.hook import uninstall
    uninstall()
    click.echo("hook uninstalled")


@hook.command(name="session-start")
def hook_session_start() -> None:
    from sagaflow.hook import format_session_start_context
    click.echo(format_session_start_context(inbox=_inbox()), nl=False)


def _ensure_hook_installed() -> None:
    from sagaflow.hook import install, is_installed
    if not is_installed():
        install()


# --- doctor probes ---


def _probe_temporal() -> tuple[str, str | None]:
    import asyncio as _a
    from sagaflow.temporal_client import TemporalUnreachable, preflight
    try:
        _a.run(preflight())
        return ("OK", None)
    except TemporalUnreachable as exc:
        return ("FAIL", str(exc))


def _probe_transport() -> tuple[str, str | None]:
    import asyncio as _a
    from sagaflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier
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
    from sagaflow.temporal_client import connect
    from sagaflow.worker import _is_worker_reachable
    try:
        async def _go() -> bool:
            client = await connect()
            return await _is_worker_reachable(client)
        running = _a.run(_go())
        return ("OK", None) if running else ("WARN", "no worker polling; will auto-spawn on launch")
    except Exception as exc:  # noqa: BLE001
        return ("FAIL", str(exc))


def _probe_hook() -> tuple[str, str | None]:
    from sagaflow.hook import is_installed
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

    from sagaflow.worker import run_worker

    _asyncio.run(run_worker())


def _ensure_worker_running() -> None:
    import asyncio as _asyncio

    from sagaflow.worker import ensure_worker_running

    _asyncio.run(ensure_worker_running())


if __name__ == "__main__":
    main()
