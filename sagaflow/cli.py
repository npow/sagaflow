"""sagaflow CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path
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


def _resolve_skill(registry, skill: str, args: dict):  # type: ignore[type-arg]  # type: ignore[type-arg]
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


async def _count_running_workflows(client: "Client", skill_prefix: str) -> int:
    count = 0
    async for wf in client.list_workflows(f"ExecutionStatus = 'Running'"):
        if wf.id.startswith(skill_prefix):
            count += 1
    return count


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

        if spec.max_concurrent > 0:
            running = await _count_running_workflows(client, effective)
            if running >= spec.max_concurrent:
                raise click.ClickException(
                    f"{effective} already has {running} running workflow(s) "
                    f"(limit: {spec.max_concurrent}). Use 'sagaflow list' to "
                    f"see them, or wait for one to finish."
                )

        # Let skills declare a deterministic ID (e.g. fix-pr-1234) so Temporal
        # rejects duplicate launches for the same logical unit.
        run_id = None
        if spec.workflow_id_fn is not None:
            run_id = spec.workflow_id_fn(args)
        if not run_id:
            run_id = f"{effective}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        from sagaflow.paths import Paths

        paths = Paths.from_env()
        paths.ensure()
        run_dir = paths.run_dir_for(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        try:
            from sagaflow.run_manifest import initialize_manifest
            initialize_manifest(
                run_dir=run_dir,
                run_id=run_id,
                skill=spec.name,
                args={k: str(v) for k, v in args.items() if not str(k).startswith("_")},
                input_path=str(args.get("path", "")) or None,
            )
        except ImportError:
            pass

        slack_channel = args.pop("_slack_channel", None)
        slack_thread_ts = args.pop("_slack_thread_ts", None)
        if slack_channel:
            from sagaflow.slack_progress import init_progress_file
            init_progress_file(
                run_dir, slack_channel, slack_thread_ts,
                skill_name=spec.name, run_id=run_id,
            )

        # Prefer the skill's own build_input if it registered one.
        if spec.build_input is not None:
            wf_input = spec.build_input(
                run_id=run_id,
                run_dir=str(run_dir),
                inbox_path=str(paths.inbox),
                cli_args=args,
            )
            from temporalio.service import RPCError
            try:
                handle = await client.start_workflow(
                    spec.workflow_cls.run,
                    wf_input,
                    id=run_id,
                    task_queue=TASK_QUEUE,
                )
            except RPCError as exc:
                if "already started" in str(exc).lower():
                    raise click.ClickException(
                        f"Workflow {run_id} is already running. "
                        f"Use 'sagaflow show {run_id}' to check status, "
                        f"or 'sagaflow abort {run_id}' to cancel it first."
                    ) from None
                raise
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


def _run_description(run_id: str) -> str:
    """Read a one-line description from concept.md, seed-topic.md, or SKILL.md in the run dir."""
    from sagaflow.paths import Paths

    run_dir = Paths.from_env().run_dir_for(run_id)
    for candidate in ("concept.md", "seed-topic.md"):
        path = run_dir / candidate
        if path.exists():
            try:
                for line in path.read_text().splitlines():
                    line = line.strip().lstrip("#").strip()
                    if line:
                        return line[:80]
            except OSError:
                pass
    return ""


def _list_workflows() -> list[dict[str, str]]:
    """Return recent sagaflow workflows from Temporal as {id, status, description} rows."""
    import asyncio as _a

    from sagaflow.temporal_client import TASK_QUEUE, connect

    async def _go() -> list[dict[str, str]]:
        client = await connect()
        rows: list[dict[str, str]] = []
        query = f"TaskQueue = '{TASK_QUEUE}'"
        async for wf in client.list_workflows(query=query):
            status = wf.status.name if wf.status is not None else "UNKNOWN"
            run_id = wf.id.removeprefix("sagaflow-")
            desc = _run_description(run_id)
            rows.append({"id": wf.id, "status": status, "description": desc})
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
@click.option("--slack-channel", default=None, help="Slack channel ID to post progress updates")
@click.option("--slack-thread-ts", default=None, help="Slack thread timestamp to reply in")
@click.pass_context
def launch(ctx: click.Context, skill: str, name: str | None, args_list: tuple[str, ...],
           path: str | None, await_result: bool,
           slack_channel: str | None, slack_thread_ts: str | None) -> None:
    """Launch a skill workflow. Non-blocking by default; --await blocks on result.

    Usage:
      sagaflow launch hello-world --name alice --await
      sagaflow launch deep-qa --path ./spec.md --arg type=doc --arg max_rounds=3
    """

    args: dict[str, object] = {}
    if name is not None:
        args["name"] = name
    if path is not None:
        args["path"] = str(Path(path).resolve())
    for kv in args_list:
        if "=" not in kv:
            raise click.UsageError(f"--arg must be key=value, got {kv!r}")
        k, _, v = kv.partition("=")
        args[k.strip()] = v.strip()
    if ctx.args:
        args["_extra"] = list(ctx.args)
    if slack_channel:
        args["_slack_channel"] = slack_channel
    if slack_thread_ts:
        args["_slack_thread_ts"] = slack_thread_ts
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
    by_status: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_status.setdefault(row["status"], []).append(row)
    order = ["RUNNING", "COMPLETED", "FAILED", "CANCELED", "TERMINATED"]
    for status in order:
        group = by_status.pop(status, [])
        if not group:
            continue
        click.echo(f"\n{status} ({len(group)}):")
        for row in group:
            rid = row["id"].removeprefix("sagaflow-")
            desc = row.get("description", "")
            if desc:
                click.echo(f"  {rid}  — {desc}")
            else:
                click.echo(f"  {rid}")
    for status, group in by_status.items():
        click.echo(f"\n{status} ({len(group)}):")
        for row in group:
            rid = row["id"].removeprefix("sagaflow-")
            desc = row.get("description", "")
            if desc:
                click.echo(f"  {rid}  — {desc}")
            else:
                click.echo(f"  {rid}")


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
    """Probe the Anthropic API with a minimal Haiku call.

    Uses the anthropic SDK directly because the pydantic-ai engine
    (engine.py get_sdk_agent / TemporalAgent) requires Temporal workflow
    context to execute. This probe runs outside any workflow, so a direct
    API call is the correct way to verify API reachability.
    """
    import asyncio as _a
    import os
    from anthropic import AsyncAnthropic
    try:
        async def _call() -> None:
            client = AsyncAnthropic(
                base_url=os.environ.get("ANTHROPIC_BASE_URL"),
                api_key=os.environ.get("ANTHROPIC_API_KEY", "sk-dummy"),
            )
            await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=8,
                system="ping",
                messages=[{"role": "user", "content": "ping"}],
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


def _probe_skill_imports() -> tuple[str, str | None]:
    """Validate all skill modules can be imported without errors."""
    from sagaflow.prompts import claude_skills_dir
    from sagaflow.worker import _build_dir_to_module_map

    skills_root = claude_skills_dir()
    if not skills_root.is_dir():
        return ("WARN", "skills directory not found")

    import importlib
    import importlib.util
    import types

    if "skills" not in sys.modules:
        pkg = types.ModuleType("skills")
        pkg.__path__ = [str(skills_root)]
        sys.modules["skills"] = pkg

    dir_to_mod = _build_dir_to_module_map(skills_root)
    failures: list[str] = []
    stubs: dict[str, tuple["Path", str]] = {}

    # Phase 1: register lightweight stubs for ALL skills (no exec_module)
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        mod_alias = dir_to_mod.get(skill_dir.name)
        if not mod_alias or mod_alias in stubs:
            continue
        init_py = skill_dir / "__init__.py"
        if not init_py.exists():
            continue
        mod_name = f"skills.{mod_alias}"
        stub = types.ModuleType(mod_name)
        stub.__path__ = [str(skill_dir)]  # type: ignore[attr-defined]
        stub.__file__ = str(init_py)
        sys.modules[mod_name] = stub
        stubs[mod_alias] = (skill_dir, mod_name)

    # Phase 2: exec_module for each skill (cross-skill imports now resolve via stubs)
    for mod_alias, (skill_dir, mod_name) in stubs.items():
        init_py = skill_dir / "__init__.py"
        try:
            spec = importlib.util.spec_from_file_location(
                mod_name, str(init_py),
                submodule_search_locations=[str(skill_dir)],
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{skill_dir.name} ({mod_alias}): {exc}")

    if failures:
        return ("FAIL", f"{len(failures)} skill(s) failed: {'; '.join(failures)}")
    return ("OK", f"{len(stubs)} skills validated")


@main.command()
def doctor() -> None:
    """Run preflight checks."""
    checks = [
        ("temporal", _probe_temporal),
        ("transport", _probe_transport),
        ("worker", _probe_worker),
        ("hook", _probe_hook),
        ("skill-imports", _probe_skill_imports),
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


# ---------------------------------------------------------------------------
# mission subcommands — absorbed from swarmd CLI
# ---------------------------------------------------------------------------


@main.group()
def mission() -> None:
    """Mission-enforced agent runner with criteria verification."""


@mission.command(name="launch")
@click.argument("mission_yaml", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--workspace",
    type=click.Path(),
    default=None,
    help="Override mission.workspace. Must be an absolute path.",
)
def mission_launch(mission_yaml: str, workspace: str | None) -> None:
    """Start a MissionWorkflow from a mission.yaml file."""
    import asyncio as _a
    import yaml
    from pathlib import Path as _Path
    from sagaflow.temporal_client import TASK_QUEUE, connect
    from sagaflow.missions.schemas.mission import Mission

    try:
        text = _Path(mission_yaml).read_text()
    except OSError as exc:
        click.echo(f"error: cannot read {mission_yaml!r}: {exc}", err=True)
        sys.exit(1)

    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        click.echo(f"error: mission YAML is malformed: {exc}", err=True)
        sys.exit(1)

    if not isinstance(data, dict):
        click.echo(f"error: mission YAML must be a mapping, got {type(data).__name__}", err=True)
        sys.exit(1)

    if workspace is not None:
        data["workspace"] = workspace

    try:
        m = Mission.model_validate(data)
    except Exception as exc:
        click.echo(f"error: mission validation failed: {exc}", err=True)
        sys.exit(1)

    from sagaflow.missions.lib.paths import ensure_session_dirs
    from datetime import datetime
    run_id = f"mission-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    ensure_session_dirs(run_id)

    async def _go() -> str:
        client = await connect()
        from datetime import timedelta
        from sagaflow.missions.workflow import MissionWorkflow
        handle = await client.start_workflow(
            MissionWorkflow.run,
            args=[m],
            id=run_id,
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(seconds=m.max_duration_sec) if m.max_duration_sec else None,
        )
        return handle.id

    _preflight_all()
    _ensure_worker_running()
    workflow_id = _a.run(_go())
    click.echo(f"workflow_id={workflow_id}")


@mission.command(name="status")
@click.argument("workflow_id")
def mission_status(workflow_id: str) -> None:
    """Query a running MissionWorkflow for its phase + criteria state."""
    import asyncio as _a
    import json
    from sagaflow.temporal_client import connect

    async def _go() -> dict:
        client = await connect()
        handle = client.get_workflow_handle(workflow_id)
        return await handle.query("get_status")

    try:
        result = _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {workflow_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: status query failed: {exc}", err=True)
        sys.exit(1)
    click.echo(json.dumps(result, indent=2, default=str))


@mission.command(name="abort")
@click.argument("workflow_id")
@click.option("--reason", default="user-abort", help="Reason for aborting.")
def mission_abort(workflow_id: str, reason: str) -> None:
    """Send the abort signal to a running MissionWorkflow."""
    import asyncio as _a
    from sagaflow.temporal_client import connect

    async def _go() -> None:
        client = await connect()
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("abort", reason)

    try:
        _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {workflow_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: abort signal failed: {exc}", err=True)
        sys.exit(1)
    click.echo(f"abort signal sent to {workflow_id}")


# ---------------------------------------------------------------------------
# chain subcommands — multi-skill pipeline orchestration
# ---------------------------------------------------------------------------


@main.group()
def chain() -> None:
    """Multi-skill pipeline orchestration via chain declarations."""


@chain.command(name="launch")
@click.argument("chain_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--input", "inputs", multiple=True, help="Chain input as key=value. Repeatable.")
@click.option("--dry-run", is_flag=True, default=False, help="Validate and print resolved inputs without executing.")
def chain_launch(chain_json: str, inputs: tuple[str, ...], dry_run: bool) -> None:
    """Start a ChainWorkflow from a chain declaration JSON file."""
    import asyncio as _a
    import json as _json
    from datetime import datetime, timedelta
    from pathlib import Path as _Path

    from sagaflow.paths import Paths
    from sagaflow.prompts import claude_skills_dir
    from sagaflow.temporal_client import TASK_QUEUE, connect

    chain_path = _Path(chain_json).resolve()
    try:
        decl = _json.loads(chain_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        click.echo(f"error: cannot read chain declaration: {exc}", err=True)
        sys.exit(1)

    if not isinstance(decl, dict):
        click.echo(f"error: chain declaration must be a JSON object, got {type(decl).__name__}", err=True)
        sys.exit(1)

    chain_id = decl.get("chain_id", chain_path.stem)
    chain_version = decl.get("version", 1)

    chain_input: dict[str, str] = {}
    for kv in inputs:
        if "=" not in kv:
            click.echo(f"error: --input must be key=value, got {kv!r}", err=True)
            sys.exit(1)
        k, v = kv.split("=", 1)
        chain_input[k] = v

    paths = Paths.from_env()
    run_id = f"chain-{chain_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_dir = paths.run_dir_for(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    from sagaflow.durable.chain_workflow import ChainWorkflow, ChainWorkflowInput

    inp = ChainWorkflowInput(
        chain_id=chain_id,
        chain_version=chain_version,
        run_dir=str(run_dir),
        chain_input=chain_input,
        initiated_by="cli",
        dry_run=dry_run,
        inbox_path=str(paths.inbox),
        skills_dir=str(claude_skills_dir()),
        chains_dir=str(chain_path.parent),
    )

    async def _go() -> str:
        client = await connect()
        handle = await client.start_workflow(
            ChainWorkflow.run,
            args=[inp],
            id=f"sagaflow-{run_id}",
            task_queue=TASK_QUEUE,
            execution_timeout=timedelta(hours=4),
        )
        return handle.id

    _preflight_all()
    _ensure_worker_running()
    workflow_id = _a.run(_go())
    click.echo(f"chain={chain_id} run_id={run_id} workflow_id={workflow_id}")
    if dry_run:
        click.echo("(dry-run mode — chain will validate and exit without executing skills)")


@chain.command(name="status")
@click.argument("run_id")
def chain_status(run_id: str) -> None:
    """Show chain progress from CHAIN_STATUS.json."""
    import json as _json

    from sagaflow.paths import Paths

    paths = Paths.from_env()
    status_file = paths.run_dir_for(run_id) / "CHAIN_STATUS.json"
    if not status_file.exists():
        click.echo(f"error: no CHAIN_STATUS.json for run {run_id!r}", err=True)
        click.echo(f"  expected at: {status_file}", err=True)
        sys.exit(1)

    try:
        status = _json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        click.echo(f"error: cannot read status file: {exc}", err=True)
        sys.exit(1)

    click.echo(_json.dumps(status, indent=2, default=str))


@chain.command(name="list")
@click.option("--dir", "chains_dir", type=click.Path(exists=True, file_okay=False), default=None,
              help="Directory to scan for chain declarations. Defaults to ~/.claude/skills/_chains/.")
def chain_list(chains_dir: str | None) -> None:
    """List available chain declarations."""
    import json as _json
    from pathlib import Path as _Path

    if chains_dir:
        scan_dir = _Path(chains_dir)
    else:
        from sagaflow.prompts import claude_skills_dir
        scan_dir = claude_skills_dir() / "_chains"

    if not scan_dir.is_dir():
        click.echo(f"No chains directory found at {scan_dir}")
        return

    found = 0
    for f in sorted(scan_dir.glob("*.json")):
        try:
            decl = _json.loads(f.read_text(encoding="utf-8"))
            cid = decl.get("chain_id", f.stem)
            desc = decl.get("description", "")
            steps = decl.get("steps", [])
            click.echo(f"  {cid}  ({len(steps)} steps)  {desc}")
            found += 1
        except (OSError, _json.JSONDecodeError):
            click.echo(f"  {f.name}  (invalid JSON)", err=True)

    if not found:
        click.echo(f"No chain declarations found in {scan_dir}")


# ---------------------------------------------------------------------------
# Intervention commands — pause / resume / inject / takeover / abort / status
# ---------------------------------------------------------------------------


def _resolve_run_workflow_id(run_id_or_workflow_id: str) -> str:
    """Accept either a sagaflow run-id (e.g. deep-qa-20260427-155146) or a
    raw Temporal workflow-id and return the Temporal workflow-id.

    Sagaflow run IDs map 1:1 to Temporal workflow IDs via the pattern
    ``sagaflow-<run_id>``.
    """
    if run_id_or_workflow_id.startswith("sagaflow-"):
        return run_id_or_workflow_id
    return f"sagaflow-{run_id_or_workflow_id}"


@main.command(name="status")
@click.argument("run_id")
def intervention_status(run_id: str) -> None:
    """Show intervention state for a running workflow."""
    import asyncio as _a
    import json

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> dict:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        return await handle.query("get_status")

    try:
        result = _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: status query failed: {exc}", err=True)
        sys.exit(1)
    click.echo(json.dumps(result, indent=2, default=str))


@main.command(name="pause")
@click.argument("run_id")
def intervention_pause(run_id: str) -> None:
    """Pause a running workflow at its next phase boundary."""
    import asyncio as _a

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> None:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("pause")

    try:
        _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: pause signal failed: {exc}", err=True)
        sys.exit(1)
    click.echo(f"pause signal sent to {run_id}")


@main.command(name="resume")
@click.argument("run_id")
def intervention_resume(run_id: str) -> None:
    """Resume a paused workflow."""
    import asyncio as _a

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> None:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("resume")

    try:
        _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: resume signal failed: {exc}", err=True)
        sys.exit(1)
    click.echo(f"resume signal sent to {run_id}")


@main.command(name="inject")
@click.argument("run_id")
@click.option("--message", "-m", required=True, help="Message to inject into the workflow context")
def intervention_inject(run_id: str, message: str) -> None:
    """Inject a message into a running or paused workflow."""
    import asyncio as _a

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> None:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("inject", message)

    try:
        _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: inject signal failed: {exc}", err=True)
        sys.exit(1)
    click.echo(f"message injected into {run_id}")


@main.command(name="takeover")
@click.argument("run_id")
def intervention_takeover(run_id: str) -> None:
    """Take over a paused workflow for manual operation."""
    import asyncio as _a

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> None:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("takeover")

    try:
        _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: takeover signal failed: {exc}", err=True)
        sys.exit(1)
    click.echo(f"takeover signal sent to {run_id}")
    click.echo("use 'sagaflow release' to return control, or 'sagaflow abort' to cancel")


@main.command(name="release")
@click.argument("run_id")
def intervention_release(run_id: str) -> None:
    """Release a taken-over workflow back to autonomous execution."""
    import asyncio as _a

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> None:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("release")

    try:
        _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: release signal failed: {exc}", err=True)
        sys.exit(1)
    click.echo(f"release signal sent to {run_id} — resuming autonomous execution")


@main.command(name="abort")
@click.argument("run_id")
@click.option("--reason", default="user-abort", help="Reason for aborting")
def intervention_abort(run_id: str, reason: str) -> None:
    """Abort a running workflow."""
    import asyncio as _a

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> None:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        await handle.signal("abort", reason)

    try:
        _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: abort signal failed: {exc}", err=True)
        sys.exit(1)
    click.echo(f"abort signal sent to {run_id}")


@main.command(name="conversation")
@click.argument("run_id")
def intervention_conversation(run_id: str) -> None:
    """Show the last 20 messages from a workflow's conversation."""
    import asyncio as _a

    from sagaflow.temporal_client import connect

    wf_id = _resolve_run_workflow_id(run_id)

    async def _go() -> list:
        client = await connect()
        handle = client.get_workflow_handle(wf_id)
        return await handle.query("get_conversation")

    try:
        msgs = _a.run(_go())
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(f"error: workflow {wf_id!r} not found", err=True)
            sys.exit(1)
        click.echo(f"error: conversation query failed: {exc}", err=True)
        sys.exit(1)
    if not msgs:
        click.echo("(no messages yet)")
        return
    for m in msgs:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str):
            preview = content[:200]
        else:
            preview = str(content)[:200]
        click.echo(f"[{role}] {preview}")


# ---------------------------------------------------------------------------
# catalog subcommands — skill capability discovery
# ---------------------------------------------------------------------------


@main.group()
def catalog() -> None:
    """Skill capability discovery catalog."""


def _get_catalog(force: bool = False) -> "SkillCatalog":  # type: ignore[name-defined]  # noqa: F821
    from sagaflow.catalog import build_catalog
    return build_catalog(force=force)


@catalog.command(name="list")
@click.option("--category", default=None, help="Filter by category enum value")
@click.option("--capability", default=None, help="Filter by capability tag")
@click.option("--maturity", default=None, help="Filter by maturity level")
def catalog_list(category: str | None, capability: str | None, maturity: str | None) -> None:
    """List skills with optional filters."""
    cat = _get_catalog()
    skills = cat.list_all(category=category, maturity=maturity, capability=capability)
    if not skills:
        click.echo("no skills match filters")
        return
    header = f"{'SKILL':<30} {'COMPLEXITY':<12} {'COST':<8} {'MATURITY':<12} CAPABILITIES"
    click.echo(header)
    for s in skills:
        caps = ", ".join(s.capabilities[:3]) if s.capabilities else "-"
        click.echo(
            f"{s.name:<30} {s.complexity or '-':<12} {s.cost_profile or '-':<8} "
            f"{s.maturity or '-':<12} {caps}"
        )


@catalog.command()
@click.argument("query")
def search(query: str) -> None:
    """Search skills by keyword."""
    cat = _get_catalog()
    results = cat.search(query)
    if not results:
        click.echo("no results")
        return
    for s, score in results[:15]:
        click.echo(f"  {s.name:<30} {score:.2f}  {s.description[:60]}")


@catalog.command(name="show")
@click.argument("name")
def catalog_show(name: str) -> None:
    """Show detailed info for a skill."""
    cat = _get_catalog()
    s = cat.show(name)
    if not s:
        click.echo(f"skill {name!r} not found")
        return
    cost_str = f", {s.cost_profile} cost" if s.cost_profile else ""
    click.echo(f"{s.name} — {s.maturity or 'unset'}, {s.complexity or 'unset'} complexity{cost_str}")
    if s.category:
        click.echo(f"  Category:     {s.category}")
    if s.capabilities:
        click.echo(f"  Capabilities: {', '.join(s.capabilities)}")
    if s.input_types:
        click.echo(f"  Input types:  {', '.join(s.input_types)}")
    if s.output_types:
        click.echo(f"  Output types: {', '.join(s.output_types)}")
    if s.output_signals:
        click.echo(f"  Output keys:  {', '.join(s.output_signals)}")
    if s.best_for:
        click.echo(f"  Best for:     {'; '.join(s.best_for)}")
    if s.not_for:
        click.echo(f"  Not for:      {'; '.join(s.not_for)}")
    if s.execution:
        parts = [f"{k}={v}" for k, v in s.execution.items()]
        click.echo(f"  Execution:    {', '.join(parts)}")
    related = cat.related(name)
    if related:
        click.echo(f"  Related:      {', '.join(r['name'] + ' (' + r.get('relation','') + ')' for r in related)}")
    if s.metadata_source:
        click.echo(f"  Metadata:     {s.metadata_source}")


@catalog.command(name="match")
@click.argument("intent")
@click.option("--input-type", default=None, help="Expected input type")
@click.option("--output-type", default=None, help="Expected output type")
def catalog_match(intent: str, input_type: str | None, output_type: str | None) -> None:
    """Match intent to skills by capability scoring."""
    cat = _get_catalog()
    results = cat.match(intent, input_type=input_type, output_type=output_type)
    if not results:
        click.echo("no matches")
        return
    click.echo(f"{'RANK':<6} {'SKILL':<25} {'SCORE':<7} REASON")
    for i, (s, score, signals) in enumerate(results[:10], 1):
        parts = [f"{k}={v:.2f}" for k, v in signals.items() if v > 0]
        click.echo(f"{i:<6} {s.name:<25} {score:<7.2f} {', '.join(parts)}")


@catalog.command()
def rebuild() -> None:
    """Force rebuild catalog from SKILL.md files."""
    from sagaflow.catalog import (
        SkillCatalog, default_skills_dir, default_enums_path,
        default_cache_path, default_lock_path, _compute_source_hash,
    )
    skills_dir = default_skills_dir()
    enums_path = default_enums_path()
    cat = SkillCatalog.from_skills_dir(skills_dir, enums_path)
    h = _compute_source_hash(skills_dir, enums_path)
    cat.save(default_cache_path(), default_lock_path(), h)
    st = cat.stats()
    click.echo(f"rebuilt: {st['total']} skills, {st['with_metadata']} with metadata")


@catalog.command()
def stats() -> None:
    """Show catalog statistics."""
    cat = _get_catalog()
    st = cat.stats()
    click.echo(f"Total skills:    {st['total']}")
    click.echo(f"With metadata:   {st['with_metadata']}")
    click.echo(f"Warnings:        {st['validation_warnings']}")
    if st["by_category"]:
        click.echo("By category:")
        for k, v in sorted(st["by_category"].items()):
            click.echo(f"  {k:<15} {v}")
    if st["by_maturity"]:
        click.echo("By maturity:")
        for k, v in sorted(st["by_maturity"].items()):
            click.echo(f"  {k:<15} {v}")


@catalog.command()
@click.option("--strict", is_flag=True, help="Treat unknown enum values as errors")
def lint(strict: bool) -> None:
    """Validate skill metadata against enum registry."""
    from sagaflow.catalog import SkillCatalog, default_skills_dir, default_enums_path
    cat = SkillCatalog.from_skills_dir(default_skills_dir(), default_enums_path())
    issues = cat.lint(strict=strict)
    if not issues:
        click.echo("no issues found")
        return
    for issue in issues:
        click.echo(f"  {issue}")
    errors = [i for i in issues if i.startswith("ERROR")]
    click.echo(f"\n{len(issues)} issues ({len(errors)} errors)")
    if strict and errors:
        sys.exit(1)


@main.command()
@click.argument("run_a")
@click.argument("run_b")
@click.option("--format", "fmt", type=click.Choice(["text", "json", "markdown"]), default="text")
def compare(run_a: str, run_b: str, fmt: str) -> None:
    """Compare two runs by run-id or path."""
    from pathlib import Path
    from sagaflow.compare import compare_runs, format_comparison
    from sagaflow.run_manifest import read_manifest
    from sagaflow.paths import Paths

    paths = Paths.from_env()
    _EXIT = {"IDENTICAL": 0, "COSMETIC_CHANGE": 0, "BEHAVIORAL_CHANGE": 1,
             "IMPROVEMENT": 1, "REGRESSION": 2, "INCOMPARABLE": 3}

    def _resolve(ref: str) -> Path:
        p = Path(ref)
        if p.is_dir():
            return p
        return paths.run_dir_for(ref)

    try:
        dir_a, dir_b = _resolve(run_a), _resolve(run_b)
        ma, mb = read_manifest(dir_a), read_manifest(dir_b)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(4)

    result = compare_runs(ma, mb)
    click.echo(format_comparison(result, fmt=fmt))
    sys.exit(_EXIT.get(result.verdict, 4))


@main.command()
@click.option("--skill", default=None, help="Filter by skill name.")
@click.option("--limit", default=20, help="Max runs to show.")
@click.option("--status", default=None, help="Filter by status (COMPLETED, FAILED).")
def history(skill: str | None, limit: int, status: str | None) -> None:
    """List runs with manifest data."""
    from sagaflow.run_manifest import read_manifest, _MANIFEST_FILE
    from sagaflow.paths import Paths

    paths = Paths.from_env()
    runs_dir = paths.runs_dir
    if not runs_dir.is_dir():
        click.echo("no runs found")
        return

    rows: list[tuple[str, str, str, str, str]] = []
    for rd in sorted(runs_dir.iterdir(), reverse=True):
        if not rd.is_dir():
            continue
        mf = rd / _MANIFEST_FILE
        if not mf.exists():
            continue
        try:
            m = read_manifest(rd)
        except Exception:
            continue
        if skill and m.skill != skill:
            continue
        if status and m.status != status.upper():
            continue
        cost = m.cost.get("estimated_cost_usd") if m.cost else None
        cost_str = f"${cost:.2f}" if cost is not None else "-"
        dur = m.timing.get("duration_seconds") if m.timing else None
        dur_str = f"{dur:.0f}s" if dur is not None else "-"
        rows.append((m.run_id, m.skill, m.status, dur_str, cost_str))
        if len(rows) >= limit:
            break

    if not rows:
        click.echo("no matching runs")
        return
    click.echo(f"{'RUN_ID':<45} {'SKILL':<20} {'STATUS':<12} {'DUR':>6} {'COST':>8}")
    click.echo("-" * 95)
    for rid, sk, st, dur_s, cost_s in rows:
        click.echo(f"{rid:<45} {sk:<20} {st:<12} {dur_s:>6} {cost_s:>8}")


@main.command()
@click.argument("run_a")
@click.argument("run_b")
def regress(run_a: str, run_b: str) -> None:
    """Check for regression between two runs. Exit 0 = no regression, 2 = regression."""
    from pathlib import Path
    from sagaflow.compare import compare_runs
    from sagaflow.run_manifest import read_manifest
    from sagaflow.paths import Paths

    paths = Paths.from_env()

    def _resolve(ref: str) -> Path:
        p = Path(ref)
        if p.is_dir():
            return p
        return paths.run_dir_for(ref)

    try:
        dir_a, dir_b = _resolve(run_a), _resolve(run_b)
        ma, mb = read_manifest(dir_a), read_manifest(dir_b)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(4)

    result = compare_runs(ma, mb)
    detail = result.termination_diff or result.verdict
    if result.verdict == "REGRESSION":
        click.echo(f"REGRESSION: {detail}")
        sys.exit(2)
    elif result.verdict == "INCOMPARABLE":
        click.echo(f"INCOMPARABLE: {detail}")
        sys.exit(3)
    else:
        click.echo(f"OK ({result.verdict}): {detail}")
        sys.exit(0)


@main.command()
@click.option("--dry-run", is_flag=True, help="Show what would be backfilled.")
@click.option("--force", is_flag=True, help="Re-backfill runs that already have manifests.")
def backfill(dry_run: bool, force: bool) -> None:
    """Backfill manifests for legacy runs."""
    from sagaflow.backfill import backfill_all
    from sagaflow.paths import Paths

    paths = Paths.from_env()
    processed = backfill_all(
        runs_dir=paths.runs_dir,
        inbox_path=paths.inbox,
        dry_run=dry_run,
        force=force,
    )
    if not processed:
        click.echo("nothing to backfill")
    else:
        verb = "would backfill" if dry_run else "backfilled"
        click.echo(f"{verb} {len(processed)} run(s)")


@main.group()
def cost() -> None:
    """Budget and cost reporting for sagaflow runs."""


@cost.command("runs")
@click.option("--limit", default=20, help="Max runs to show.")
@click.option("--skill", default=None, help="Filter by skill name.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def cost_runs(limit: int, skill: str | None, fmt: str) -> None:
    """Show cost breakdown per run (most recent first)."""
    import json as _json

    from sagaflow.paths import Paths

    runs_dir = Paths.from_env().runs_dir
    if not runs_dir.exists():
        click.echo("no runs found")
        return

    rows: list[dict] = []
    for d in sorted(runs_dir.iterdir(), reverse=True):
        mf = d / "run_manifest.json"
        if not mf.exists():
            continue
        data = _json.loads(mf.read_text())
        if skill and data.get("skill") != skill:
            continue
        c = data.get("cost", {})
        rows.append({
            "run_id": data.get("run_id", d.name),
            "skill": data.get("skill", ""),
            "status": data.get("status", ""),
            "cost_usd": c.get("estimated_cost_usd", 0.0),
            "input_tokens": c.get("total_input_tokens", 0),
            "output_tokens": c.get("total_output_tokens", 0),
            "steps": len(data.get("steps", [])),
        })
        if len(rows) >= limit:
            break

    if not rows:
        click.echo("no runs with cost data")
        return

    if fmt == "json":
        click.echo(_json.dumps(rows, indent=2))
        return

    click.echo(f"{'RUN ID':<45} {'SKILL':<20} {'COST':>10} {'STEPS':>6} {'STATUS':<10}")
    click.echo("-" * 95)
    total = 0.0
    for r in rows:
        total += r["cost_usd"]
        click.echo(
            f"{r['run_id']:<45} {r['skill']:<20} "
            f"${r['cost_usd']:>8.4f} {r['steps']:>6} {r['status']:<10}"
        )
    click.echo("-" * 95)
    click.echo(f"{'TOTAL':<45} {'':<20} ${total:>8.4f}")


@cost.command("skills")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def cost_skills(fmt: str) -> None:
    """Aggregate cost by skill across all runs."""
    import json as _json
    from collections import defaultdict

    from sagaflow.paths import Paths

    runs_dir = Paths.from_env().runs_dir
    if not runs_dir.exists():
        click.echo("no runs found")
        return

    agg: dict[str, dict] = defaultdict(lambda: {
        "runs": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
    })
    for d in runs_dir.iterdir():
        mf = d / "run_manifest.json"
        if not mf.exists():
            continue
        data = _json.loads(mf.read_text())
        sk = data.get("skill", "unknown")
        c = data.get("cost", {})
        entry = agg[sk]
        entry["runs"] += 1
        entry["cost_usd"] += c.get("estimated_cost_usd", 0.0)
        entry["input_tokens"] += c.get("total_input_tokens", 0)
        entry["output_tokens"] += c.get("total_output_tokens", 0)

    if not agg:
        click.echo("no runs with cost data")
        return

    rows = sorted(agg.items(), key=lambda x: x[1]["cost_usd"], reverse=True)

    if fmt == "json":
        click.echo(_json.dumps({k: v for k, v in rows}, indent=2))
        return

    click.echo(f"{'SKILL':<30} {'RUNS':>6} {'TOTAL COST':>12} {'AVG COST':>10}")
    click.echo("-" * 62)
    for sk, v in rows:
        avg = v["cost_usd"] / v["runs"] if v["runs"] else 0
        click.echo(f"{sk:<30} {v['runs']:>6} ${v['cost_usd']:>10.4f} ${avg:>8.4f}")


@cost.command("top")
@click.option("-n", default=10, help="Number of runs to show.")
def cost_top(n: int) -> None:
    """Show the N most expensive runs."""
    import json as _json

    from sagaflow.paths import Paths

    runs_dir = Paths.from_env().runs_dir
    if not runs_dir.exists():
        click.echo("no runs found")
        return

    entries: list[tuple[float, str, str]] = []
    for d in runs_dir.iterdir():
        mf = d / "run_manifest.json"
        if not mf.exists():
            continue
        data = _json.loads(mf.read_text())
        c = data.get("cost", {}).get("estimated_cost_usd", 0.0)
        entries.append((c, data.get("run_id", d.name), data.get("skill", "")))

    entries.sort(reverse=True)

    click.echo(f"{'#':>3} {'RUN ID':<45} {'SKILL':<20} {'COST':>10}")
    click.echo("-" * 82)
    for i, (c, rid, sk) in enumerate(entries[:n], 1):
        click.echo(f"{i:>3} {rid:<45} {sk:<20} ${c:>8.4f}")


@cost.command("daily")
@click.option("--days", default=7, help="Number of days to show.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def cost_daily(days: int, fmt: str) -> None:
    """Show daily cost aggregation."""
    import json as _json
    import re
    from collections import defaultdict

    from sagaflow.paths import Paths

    runs_dir = Paths.from_env().runs_dir
    if not runs_dir.exists():
        click.echo("no runs found")
        return

    daily: dict[str, dict] = defaultdict(lambda: {"runs": 0, "cost_usd": 0.0})
    date_re = re.compile(r"\d{8}")

    for d in runs_dir.iterdir():
        mf = d / "run_manifest.json"
        if not mf.exists():
            continue
        data = _json.loads(mf.read_text())
        m = date_re.search(d.name)
        day = f"{m.group()[:4]}-{m.group()[4:6]}-{m.group()[6:8]}" if m else "unknown"
        c = data.get("cost", {}).get("estimated_cost_usd", 0.0)
        daily[day]["runs"] += 1
        daily[day]["cost_usd"] += c

    rows = sorted(daily.items(), reverse=True)[:days]

    if fmt == "json":
        click.echo(_json.dumps({k: v for k, v in rows}, indent=2))
        return

    click.echo(f"{'DATE':<12} {'RUNS':>6} {'COST':>12}")
    click.echo("-" * 34)
    total = 0.0
    for day, v in rows:
        total += v["cost_usd"]
        click.echo(f"{day:<12} {v['runs']:>6} ${v['cost_usd']:>10.4f}")
    click.echo("-" * 34)
    click.echo(f"{'TOTAL':<12} {'':<6} ${total:>10.4f}")


@main.group()
def replay() -> None:
    """Deterministic replay: record and replay workflow runs without LLM calls."""


@replay.command("list")
@click.option("--limit", default=20, help="Max cassettes to show.")
def replay_list(limit: int) -> None:
    """List runs that have recorded cassettes."""
    from sagaflow.paths import Paths
    from sagaflow.replay.cassette import list_cassettes

    runs_dir = Paths.from_env().runs_dir
    cassettes = list_cassettes(runs_dir)
    if not cassettes:
        click.echo("no cassettes found")
        return

    click.echo(f"{'RUN ID':<45} {'SKILL':<20} {'STEPS':>6} {'RECORDED'}")
    click.echo("-" * 90)
    for c in cassettes[:limit]:
        click.echo(
            f"{c['run_id']:<45} {c['skill']:<20} {c['entries']:>6} {c['recorded_at']}"
        )


@replay.command("show")
@click.argument("run_id")
def replay_show(run_id: str) -> None:
    """Show cassette details for a run."""
    from sagaflow.paths import Paths
    from sagaflow.replay.cassette import load

    run_dir = Paths.from_env().run_dir_for(run_id)
    try:
        cassette = load(run_dir)
    except FileNotFoundError:
        raise click.UsageError(f"no cassette for run {run_id}") from None

    click.echo(f"Run:      {cassette.run_id}")
    click.echo(f"Skill:    {cassette.skill}")
    click.echo(f"Recorded: {cassette.recorded_at}")
    click.echo(f"Entries:  {len(cassette.entries)}")
    click.echo()
    click.echo(f"{'#':>3} {'ROLE':<25} {'TIER':<10} {'DURATION':>8} {'INPUT HASH'}")
    click.echo("-" * 70)
    for e in cassette.entries:
        click.echo(
            f"{e.seq + 1:>3} {e.role:<25} {e.tier:<10} {e.duration_seconds:>7.1f}s {e.input_hash}"
        )


@replay.command("run")
@click.argument("run_id")
@click.option("--target", default=None, help="Temporal server address override")
def replay_run(run_id: str, target: str | None) -> None:
    """Re-execute a workflow using its recorded cassette (no LLM calls)."""
    import asyncio as _a

    from sagaflow.paths import Paths
    from sagaflow.replay.worker import run_replay_worker

    run_dir = Paths.from_env().run_dir_for(run_id)
    click.echo(f"Starting replay for {run_id} ...")
    _a.run(run_replay_worker(run_dir, target=target))


@main.group()
def portfolio() -> None:
    """Portfolio evaluation: ROI scoring, cost analysis, and skill lifecycle."""


@portfolio.command("init")
def portfolio_init() -> None:
    """Create portfolio.db and apply schema migrations. Safe to re-run."""
    from sagaflow.portfolio.db import init_db

    path = init_db()
    click.echo(f"Portfolio DB initialized at {path}")


@portfolio.command("summary")
@click.option("--window", default=90, type=int, help="Scoring window in days.")
def portfolio_summary(window: int) -> None:
    """Show ROI summary for all skills."""
    from sagaflow.portfolio.db import db_exists
    from sagaflow.portfolio.scorer import ROIScorer

    if not db_exists():
        click.echo("error: portfolio DB not found. Run 'sagaflow portfolio init' first.", err=True)
        sys.exit(1)

    scorer = ROIScorer(window_days=window)
    scores = scorer.score_all()
    if not scores:
        click.echo("No invocation data found.")
        return

    scores.sort(key=lambda s: (s.insufficient_data, -(s.composite or 0)))

    click.echo(
        f"{'SKILL':<30} {'VERDICT':<22} {'COMPOSITE':>9} {'RUNS':>6} {'LAST USED':<20}"
    )
    click.echo("-" * 92)
    for s in scores:
        composite_str = f"{s.composite:.3f}" if s.composite is not None else "—"
        verdict_str = s.verdict.value if s.verdict else "insufficient_data"
        last_used = s.computed_at.strftime("%Y-%m-%d") if s.computed_at else "—"
        click.echo(
            f"{s.skill_name:<30} {verdict_str:<22} {composite_str:>9} "
            f"{s.sample_count:>6} {last_used:<20}"
        )


@portfolio.command("inspect")
@click.argument("skill_name")
@click.option("--window", default=90, type=int, help="Scoring window in days.")
def portfolio_inspect(skill_name: str, window: int) -> None:
    """Full drill-down for a single skill's ROI scores."""
    from sagaflow.portfolio.costs import CostAggregator, TimeWindow
    from sagaflow.portfolio.db import db_exists
    from sagaflow.portfolio.retirement import RetirementAdvisor
    from sagaflow.portfolio.scorer import ROIScorer

    if not db_exists():
        click.echo("error: portfolio DB not found. Run 'sagaflow portfolio init' first.", err=True)
        sys.exit(1)

    scorer = ROIScorer(window_days=window)
    s = scorer.score(skill_name)

    click.echo(f"Skill:      {s.skill_name}")
    click.echo(f"Verdict:    {s.verdict.value if s.verdict else 'insufficient_data'}")
    click.echo(f"Composite:  {s.composite:.4f}" if s.composite is not None else "Composite:  —")
    click.echo(f"Samples:    {s.sample_count} ({'insufficient' if s.insufficient_data else 'sufficient'})")
    click.echo()
    click.echo("Sub-scores:")
    click.echo(f"  usage      (w=0.25): {s.usage_score:.4f}")
    click.echo(f"  recency    (w=0.20): {s.recency_score:.4f}")
    click.echo(
        f"  outcome    (w=0.35): {s.outcome_score:.4f}"
        if s.outcome_score is not None
        else "  outcome    (w=0.35): —"
    )
    click.echo(
        f"  cost_eff   (w=0.20): {s.cost_efficiency_score:.4f}"
        if s.cost_efficiency_score is not None
        else "  cost_eff   (w=0.20): —"
    )

    tw = TimeWindow.last_days(window)
    cost_agg = CostAggregator()
    cost = cost_agg.cost_for_skill(skill_name, tw)
    click.echo()
    click.echo(f"Cost ({window}d): ${cost.total_usd:.4f} total, ${cost.avg_usd_per_run:.4f} avg/run, {cost.run_count} runs")

    advisor = RetirementAdvisor()
    rec = advisor.recommendation_for(skill_name)
    if rec:
        click.echo()
        click.echo(f"Retirement: {rec.criterion_triggered} → {rec.recommended_transition} ({rec.confidence})")
        click.echo(f"  {rec.narrative}")


@portfolio.command("trends")
@click.option("--window", default=90, type=int, help="Window in days.")
@click.option("--skill", default=None, help="Filter to a single skill.")
@click.option(
    "--granularity",
    type=click.Choice(["day", "week", "month"]),
    default="week",
    help="Time bucket granularity.",
)
def portfolio_trends(window: int, skill: str | None, granularity: str) -> None:
    """Show cost trends over time."""
    from sagaflow.portfolio.costs import CostAggregator, TimeWindow
    from sagaflow.portfolio.db import db_exists

    if not db_exists():
        click.echo("error: portfolio DB not found. Run 'sagaflow portfolio init' first.", err=True)
        sys.exit(1)

    tw = TimeWindow.last_days(window)
    agg = CostAggregator()

    if skill:
        skills_to_show = [skill]
    else:
        by_skill = agg.cost_by_skill(tw)
        skills_to_show = [name for name, _ in by_skill[:10]]

    for name in skills_to_show:
        points = agg.cost_trend(name, granularity=granularity, window=tw)
        if not points:
            continue
        click.echo(f"\n{name}:")
        click.echo(f"  {'PERIOD':<14} {'RUNS':>6} {'COST':>10}")
        for p in points:
            click.echo(f"  {p.period_start:<14} {p.run_count:>6} ${p.total_usd:>9.4f}")


@portfolio.command("retire")
@click.argument("skill_name")
@click.option(
    "--transition",
    required=True,
    type=click.Choice(["deprecated", "deleted"]),
    help="Target lifecycle state.",
)
@click.option("--note", default=None, help="Optional note for the lifecycle event.")
@click.option("--dry-run", is_flag=True, help="Print what would be written without committing.")
def portfolio_retire(skill_name: str, transition: str, note: str | None, dry_run: bool) -> None:
    """Record a retirement lifecycle event for a skill (advisory only)."""
    from sagaflow.portfolio.db import db_exists, get_connection

    if not db_exists():
        click.echo("error: portfolio DB not found. Run 'sagaflow portfolio init' first.", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"[dry-run] Would write lifecycle event: {skill_name} → {transition}")
        if note:
            click.echo(f"  note: {note}")
        return

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO lifecycle_events (skill_name, to_state, transition, operator, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (skill_name, transition, transition, "cli", note),
        )
        conn.commit()
    finally:
        conn.close()
    click.echo(f"Lifecycle event recorded: {skill_name} → {transition}")


@portfolio.command("snapshot")
@click.option("--name", required=True, help="Snapshot name (e.g., baseline-v1).")
def portfolio_snapshot(name: str) -> None:
    """Save current ROI scores to a JSON snapshot file."""
    import json
    from sagaflow.portfolio.db import db_exists
    from sagaflow.portfolio.scorer import ROIScorer

    if not db_exists():
        click.echo("error: portfolio DB not found. Run 'sagaflow portfolio init' first.", err=True)
        sys.exit(1)

    snap_dir = Path.home() / ".sagaflow" / "portfolio_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / f"{name}.json"

    scorer = ROIScorer()
    scores = scorer.score_all()
    data = {
        s.skill_name: {
            "composite": s.composite,
            "verdict": s.verdict.value if s.verdict else None,
            "usage": s.usage_score,
            "recency": s.recency_score,
            "outcome": s.outcome_score,
            "cost_eff": s.cost_efficiency_score,
            "sample_count": s.sample_count,
            "computed_at": s.computed_at.isoformat(),
        }
        for s in scores
    }
    snap_file.write_text(json.dumps(data, indent=2))
    click.echo(f"Snapshot saved: {snap_file}")


@portfolio.command("regress")
@click.option("--baseline", required=True, help="Snapshot name to compare against.")
@click.option("--threshold", default=0.1, type=float, help="Max allowed score drop.")
def portfolio_regress(baseline: str, threshold: float) -> None:
    """Compare current scores against a baseline snapshot. Exits 1 on regression."""
    import json
    from sagaflow.portfolio.db import db_exists
    from sagaflow.portfolio.scorer import ROIScorer

    if not db_exists():
        click.echo("error: portfolio DB not found. Run 'sagaflow portfolio init' first.", err=True)
        sys.exit(1)

    snap_file = Path.home() / ".sagaflow" / "portfolio_snapshots" / f"{baseline}.json"
    if not snap_file.exists():
        click.echo(f"error: snapshot {baseline!r} not found at {snap_file}", err=True)
        sys.exit(1)

    baseline_data = json.loads(snap_file.read_text())

    scorer = ROIScorer()
    current = {s.skill_name: s for s in scorer.score_all()}

    regressions: list[str] = []
    for name, prev in baseline_data.items():
        if prev.get("composite") is None:
            continue
        cur = current.get(name)
        if cur is None or cur.composite is None:
            continue
        delta = prev["composite"] - cur.composite
        if delta > threshold:
            regressions.append(
                f"  {name}: {prev['composite']:.3f} → {cur.composite:.3f} (Δ={delta:+.3f})"
            )

    if regressions:
        click.echo(f"REGRESSION DETECTED (threshold={threshold}):", err=True)
        for line in regressions:
            click.echo(line, err=True)
        sys.exit(1)
    else:
        click.echo(f"No regressions (threshold={threshold}, baseline={baseline}).")


@main.group()
def memory() -> None:
    """Cross-session skill memory: outcomes, recall, patterns."""


def _get_memory_db() -> "SkillMemoryDB":  # type: ignore[name-defined]  # noqa: F821
    from sagaflow.memory.db import SkillMemoryDB
    return SkillMemoryDB.open()


@memory.command(name="list")
@click.option("--skill", default=None, help="Filter by skill name.")
@click.option("--limit", default=20, type=int, help="Max outcomes to show.")
def memory_list(skill: str | None, limit: int) -> None:
    """List recent outcome records."""
    db = _get_memory_db()
    try:
        outcomes = db.list_outcomes(skill=skill, limit=limit)
    finally:
        db.close()
    if not outcomes:
        click.echo("no outcomes recorded")
        return
    click.echo(f"{'RUN_ID':<45} {'SKILL':<20} {'LABEL':<18} {'DUR':>6} {'COST':>8}")
    click.echo("-" * 100)
    for o in outcomes:
        cost_str = f"${o.cost_usd:.2f}" if o.cost_usd else "-"
        click.echo(
            f"{o.run_id:<45} {o.skill:<20} {o.terminal_label:<18} "
            f"{o.duration_s:>5.0f}s {cost_str:>8}"
        )


@memory.command(name="show")
@click.argument("run_id")
def memory_show(run_id: str) -> None:
    """Show full details for a single outcome."""
    db = _get_memory_db()
    try:
        o = db.get_outcome(run_id)
    finally:
        db.close()
    if not o:
        click.echo(f"outcome {run_id!r} not found")
        return
    click.echo(f"Run ID:     {o.run_id}")
    click.echo(f"Skill:      {o.skill}")
    click.echo(f"Label:      {o.terminal_label}")
    click.echo(f"Started:    {o.started_at}")
    click.echo(f"Completed:  {o.completed_at}")
    click.echo(f"Duration:   {o.duration_s:.0f}s")
    cost_str = f"${o.cost_usd:.2f}" if o.cost_usd else "-"
    click.echo(f"Cost:       {cost_str}")
    if o.input_tokens or o.output_tokens:
        click.echo(f"Tokens:     {o.input_tokens or 0} in / {o.output_tokens or 0} out")
    if o.input_hash:
        click.echo(f"Input hash: {o.input_hash}")
    if o.primary_artifact:
        click.echo(f"Artifact:   {o.primary_artifact}")
    if o.sagaflow_version:
        click.echo(f"Version:    {o.sagaflow_version}")
    if o.findings_text:
        click.echo(f"\nFindings:\n{o.findings_text[:1000]}")


@memory.command(name="search")
@click.argument("query")
@click.option("--skill", default=None, help="Filter by skill name.")
@click.option("--limit", default=10, type=int, help="Max results.")
def memory_search(query: str, skill: str | None, limit: int) -> None:
    """Full-text search across outcome findings."""
    db = _get_memory_db()
    try:
        results = db.query_outcomes(query=query, skill=skill, limit=limit)
    finally:
        db.close()
    if not results:
        click.echo("no matches")
        return
    for o in results:
        click.echo(f"  {o.run_id:<40} {o.skill:<16} {o.findings_text[:80]}")


@memory.command(name="patterns")
@click.option("--skill", default=None, help="Filter by skill.")
@click.option("--min-freq", default=1, type=int, help="Minimum frequency.")
def memory_patterns(skill: str | None, min_freq: int) -> None:
    """List promoted patterns."""
    db = _get_memory_db()
    try:
        pats = db.query_patterns(skill=skill, min_frequency=min_freq)
    finally:
        db.close()
    if not pats:
        click.echo("no patterns")
        return
    click.echo(f"{'SKILL':<20} {'TYPE':<16} {'KEY':<30} {'FREQ':>5} {'CONF':<8}")
    for p in pats:
        click.echo(f"{p.skill:<20} {p.pattern_type:<16} {p.pattern_key:<30} {p.frequency:>5} {p.confidence:<8}")


@memory.command(name="stats")
def memory_stats() -> None:
    """Show memory database statistics."""
    db = _get_memory_db()
    try:
        total = db.count_outcomes()
        skills: dict[str, int] = {}
        for o in db.list_outcomes(limit=10000):
            skills[o.skill] = skills.get(o.skill, 0) + 1
        patterns = db.query_patterns()
    finally:
        db.close()
    click.echo(f"Total outcomes: {total}")
    click.echo(f"Distinct skills: {len(skills)}")
    if skills:
        click.echo("\nOutcomes per skill:")
        for sk, count in sorted(skills.items(), key=lambda x: -x[1]):
            click.echo(f"  {sk:<30} {count}")
    click.echo(f"\nPatterns: {len(patterns)}")


@main.group()
def test():
    """Run scenario reliability tests."""


@test.command(name="run")
@click.argument("skills", nargs=-1)
@click.option("-v", "--verbose", is_flag=True, help="Pass -v to pytest.")
@click.option("--report", type=click.Choice(["json", "console"]), default="console",
              help="Output format.")
@click.option("--output", "output_path", type=click.Path(), default=None,
              help="Write JSON report to FILE.")
@click.option("--save-baseline", is_flag=True,
              help="Save results as ~/.sagaflow/baselines/latest.json")
@click.option("--fail-fast", is_flag=True, help="Stop on first failure.")
def test_run(skills, verbose, report, output_path, save_baseline, fail_fast):
    """Run scenario tests, optionally filtered by SKILL names."""
    import subprocess
    import sys
    import tempfile

    scenario_dir = Path(__file__).resolve().parent.parent / "tests" / "scenarios"
    if not scenario_dir.is_dir():
        click.echo(f"Scenario directory not found: {scenario_dir}", err=True)
        raise SystemExit(1)

    if skills:
        targets = []
        for sk in skills:
            normalized = sk.replace("-", "_")
            candidate = scenario_dir / f"test_{normalized}.py"
            if candidate.exists():
                targets.append(str(candidate))
            else:
                click.echo(f"No scenario file for skill '{sk}': {candidate}", err=True)
                raise SystemExit(1)
    else:
        targets = [str(scenario_dir)]

    report_file = output_path
    if report == "json" and not report_file:
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        report_file = tmp.name

    args = [sys.executable, "-m", "pytest", *targets]
    if verbose:
        args.append("-v")
    if fail_fast:
        args.append("-x")
    if report_file:
        args.append(f"--scenario-report={report_file}")

    result = subprocess.run(args, cwd=str(scenario_dir.parent.parent))

    if report_file and report == "json":
        rpath = Path(report_file)
        if rpath.exists():
            click.echo(rpath.read_text())

    if save_baseline and report_file:
        baseline_dir = Path.home() / ".sagaflow" / "baselines"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        dest = baseline_dir / "latest.json"
        rpath = Path(report_file)
        if rpath.exists():
            dest.write_text(rpath.read_text())
            click.echo(f"Baseline saved: {dest}")

    raise SystemExit(result.returncode)


@test.command(name="compare")
@click.argument("baseline", type=click.Path(exists=True))
@click.argument("current", type=click.Path(exists=True))
def test_compare(baseline, current):
    """Compare two scenario report JSON files."""
    from tests.scenarios.reporter import compare_reports as _compare

    diff = _compare(Path(baseline), Path(current))
    click.echo(f"Baseline: {diff['baseline_total']} scenarios")
    click.echo(f"Current:  {diff['current_total']} scenarios")
    if diff["regressions"]:
        click.secho(f"\nRegressions ({len(diff['regressions'])}):", fg="red", bold=True)
        for name in diff["regressions"]:
            click.echo(f"  FAIL  {name}")
    if diff["improvements"]:
        click.secho(f"\nImprovements ({len(diff['improvements'])}):", fg="green", bold=True)
        for name in diff["improvements"]:
            click.echo(f"  PASS  {name}")
    if diff["new_scenarios"]:
        click.echo(f"\nNew ({len(diff['new_scenarios'])}): {', '.join(diff['new_scenarios'])}")
    if diff["removed"]:
        click.echo(f"\nRemoved ({len(diff['removed'])}): {', '.join(diff['removed'])}")
    if diff["has_regressions"]:
        raise SystemExit(1)
    click.secho("\nNo regressions.", fg="green")


@test.command(name="list")
@click.option("--skill", default=None, help="Filter by skill name.")
def test_list(skill):
    """List all registered scenario tests."""
    import importlib
    import pkgutil
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    scenario_pkg_path = repo_root / "tests" / "scenarios"
    if not scenario_pkg_path.is_dir():
        click.echo("Scenario directory not found.", err=True)
        raise SystemExit(1)

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    importlib.import_module("tests.conftest")

    for _importer, modname, _ispkg in pkgutil.iter_modules([str(scenario_pkg_path)]):
        if modname.startswith("test_"):
            importlib.import_module(f"tests.scenarios.{modname}")

    from tests.scenarios.registry import SCENARIO_REGISTRY

    entries = sorted(SCENARIO_REGISTRY.values(), key=lambda m: (m.skill, m.name))
    if skill:
        entries = [e for e in entries if e.skill == skill]

    if not entries:
        click.echo("No scenarios found.")
        return

    click.echo(f"{'SKILL':<25} {'SCENARIO':<40} {'MODES'}")
    click.echo("-" * 90)
    for e in entries:
        modes = ", ".join(e.failure_modes[:3]) or "-"
        click.echo(f"{e.skill:<25} {e.name:<40} {modes}")
    click.echo(f"\nTotal: {len(entries)} scenarios")


if __name__ == "__main__":
    main()
