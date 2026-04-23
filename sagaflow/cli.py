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


def _resolve_skill(registry, skill: str, args: dict):  # type: ignore[type-arg]
    """Return the SkillSpec for ``skill``, falling back to the generic interpreter.

    Mutates ``args`` in-place: when the fallback fires, sets ``_target_skill`` so
    ``skills.generic._build_input`` knows which claude-skill to load.

    Raises ``click.UsageError`` (shape matches pre-existing callers' expectations)
    if neither a registered skill nor a claude-skills SKILL.md exists for the name.
    """
    from sagaflow.prompts import claude_skills_dir

    try:
        return registry.get(skill)
    except KeyError:
        pass
    claude_skill_md = claude_skills_dir() / skill / "SKILL.md"
    if not claude_skill_md.exists():
        raise click.UsageError(
            f"unknown skill: {skill!r}; no SKILL.md at {claude_skill_md}"
        ) from None
    try:
        spec = registry.get("generic")
    except KeyError as exc:
        raise click.UsageError(
            f"unknown skill: {skill!r}; generic interpreter not registered "
            f"(sagaflow.generic.workflow may be missing)"
        ) from exc
    args["_target_skill"] = skill
    return spec


def _start_workflow(skill: str, args: dict) -> str:  # type: ignore[type-arg]
    import asyncio as _a
    from datetime import datetime
    from sagaflow.temporal_client import TASK_QUEUE, connect
    from sagaflow.worker import build_registry

    async def _go() -> str:
        client = await connect()
        registry = build_registry()
        spec = _resolve_skill(registry, skill, args)
        # Use the (possibly fallback-rewritten) spec.name for the run id so run
        # ids always reflect the skill actually invoked.
        effective = spec.name
        run_id = f"{effective}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        from sagaflow.paths import Paths

        paths = Paths.from_env()
        paths.ensure()
        run_dir = paths.run_dir_for(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        # Prefer the skill's own build_input if it registered one.
        if spec.build_input is not None:
            wf_input = spec.build_input(
                run_id=run_id,
                run_dir=str(run_dir),
                inbox_path=str(paths.inbox),
                cli_args=args,
            )
            handle = await client.start_workflow(
                spec.workflow_cls.run,
                wf_input,
                id=run_id,
                task_queue=TASK_QUEUE,
            )
            return handle.id

        # Back-compat path for hello-world, which registered before build_input existed.
        if effective == "hello-world":
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
        raise NotImplementedError(f"launch wiring missing for skill {effective!r}")

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
    """Return recent sagaflow workflows from Temporal as {id, status} rows."""
    import asyncio as _a

    from sagaflow.temporal_client import TASK_QUEUE, connect

    async def _go() -> list[dict[str, str]]:
        client = await connect()
        rows: list[dict[str, str]] = []
        query = f"TaskQueue = '{TASK_QUEUE}'"
        async for wf in client.list_workflows(query=query):
            status = wf.status.name if wf.status is not None else "UNKNOWN"
            rows.append({"id": wf.id, "status": status})
        return rows

    try:
        return _a.run(_go())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"warning: could not list workflows: {exc}", err=True)
        return []


@main.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    )
)
@click.argument("skill")
@click.option("--name", default=None, help="hello-world back-compat: greeting target name")
@click.option("--arg", "args_list", multiple=True, metavar="KEY=VALUE",
              help="Skill-specific argument. Repeat for multiple: --arg key=value --arg k2=v2")
@click.option("--path", default=None, help="Path input (artifact/spec/task file) for skills that take one")
@click.option("--await", "await_result", is_flag=True, help="Block until the workflow finishes")
@click.pass_context
def launch(ctx: click.Context, skill: str, name: str | None, args_list: tuple[str, ...],
           path: str | None, await_result: bool) -> None:
    """Launch a skill workflow. Non-blocking by default; --await blocks on result.

    Usage:
      sagaflow launch hello-world --name alice --await
      sagaflow launch deep-qa --path ./spec.md --arg type=doc --arg max_rounds=3
    """

    args: dict[str, object] = {}
    if name is not None:
        args["name"] = name
    if path is not None:
        args["path"] = path
    for kv in args_list:
        if "=" not in kv:
            raise click.UsageError(f"--arg must be key=value, got {kv!r}")
        k, _, v = kv.partition("=")
        args[k.strip()] = v.strip()
    if ctx.args:
        args["_extra"] = list(ctx.args)
    if skill == "hello-world" and "name" not in args:
        args["name"] = "world"

    _validate_skill_and_args(skill, args)

    _preflight_all()
    _ensure_hook_installed()
    _ensure_worker_running()

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


def _validate_skill_and_args(skill: str, args: dict) -> None:  # type: ignore[type-arg]
    """Surface bad CLI invocations as UsageError before spending time on Temporal.

    Catches unknown skill names and any ``ValueError`` raised by a skill's
    ``build_input`` (the canonical place skills declare required args). Also
    applies the generic-interpreter fallback: if ``skill`` isn't registered but
    ``~/.claude/skills/<skill>/SKILL.md`` exists, route to the generic skill
    (mutating ``args`` to stash ``_target_skill``).
    """
    from sagaflow.worker import build_registry

    registry = build_registry()
    spec = _resolve_skill(registry, skill, args)
    if spec.build_input is None:
        return
    try:
        spec.build_input(
            run_id="__sagaflow_validate__",
            run_dir="/tmp/__sagaflow_validate__",
            inbox_path="/tmp/__sagaflow_validate_inbox__.md",
            cli_args=args,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None


if __name__ == "__main__":
    main()
