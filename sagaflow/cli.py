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

        from sagaflow.manifest import initialize_manifest
        initialize_manifest(
            run_dir=run_dir,
            run_id=run_id,
            skill=spec.name,
            args={k: str(v) for k, v in args.items() if not str(k).startswith("_")},
            input_path=str(args.get("path", "")) or None,
        )

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
        args["path"] = path
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
# catalog subcommands — skill capability discovery
# ---------------------------------------------------------------------------


@main.group()
def catalog() -> None:
    """Skill capability discovery catalog."""


def _get_catalog(force: bool = False) -> "SkillCatalog":  # type: ignore[name-defined]
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
    from sagaflow.manifest import read_manifest
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
    from sagaflow.manifest import read_manifest, _MANIFEST_FILE
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
    from sagaflow.manifest import read_manifest
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


if __name__ == "__main__":
    main()
