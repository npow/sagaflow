"""Worker daemon: registers all skills and polls the shared task queue."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import logging.handlers
import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from sagaflow.paths import Paths
from sagaflow.prompts import claude_skills_dir
from sagaflow.registry import SkillRegistry
from sagaflow.temporal_client import DEFAULT_NAMESPACE, DEFAULT_TARGET, TASK_QUEUE, connect

_log = logging.getLogger(__name__)


def _configure_worker_logging() -> None:
    """Set up structured logging to stderr + rotating file.

    Must be called early in run_worker() so every module's getLogger(__name__)
    calls actually produce output.
    """
    log_dir = Paths.from_env().worker_log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "worker.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=50 * 1024 * 1024, backupCount=3,
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(stderr_handler)
    root.addHandler(file_handler)

    logging.getLogger("sagaflow").setLevel(logging.INFO)
    logging.getLogger("temporalio").setLevel(logging.WARNING)

_PASSTHROUGH_MODULES = ("httpx", "anthropic", "sagaflow", "pydantic", "pydantic_ai", "skills", "claude_skill_", "sniffio")

_ACTIVITY_MEM_BUDGET_MB = 600


def _default_max_activities() -> int:
    """Compute max concurrent activities from available memory.

    Each spawn_subagent activity can launch a Claude CLI process (~400-600MB).
    Reserve 4GB for OS + Temporal + worker overhead, divide the rest by
    per-activity budget, and clamp to [2, 32].
    """
    _OS_RESERVE_MB = 4096
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) / 1024
                    break
            else:
                return 8
        available_mb = max(0, total_mb - _OS_RESERVE_MB)
        return max(2, min(32, int(available_mb // _ACTIVITY_MEM_BUDGET_MB)))
    except Exception:
        return 8


def _build_sandbox_runner() -> SandboxedWorkflowRunner:
    """Sandbox runner with httpx/anthropic/sagaflow allowed through import validation."""

    restrictions = SandboxRestrictions.default.with_passthrough_modules(*_PASSTHROUGH_MODULES)
    return SandboxedWorkflowRunner(restrictions=restrictions)


def _ensure_skills_package() -> None:
    """Ensure ``skills`` is a namespace package in ``sys.modules``.

    Cross-skill workflow imports (e.g. autopilot -> deep_plan) use
    ``from skills.<name>.workflow import ...``. We register each loaded
    skill under ``skills.<legacy_name>`` so these imports resolve.

    The repo's ``skills/`` directory is kept on ``__path__`` so that
    ``from skills import generic`` (the in-repo generic interpreter)
    still works alongside dynamically-loaded claude-skills modules.
    """
    from pathlib import Path
    repo_skills = str(Path(__file__).resolve().parent.parent / "skills")
    if "skills" not in sys.modules:
        import types
        pkg = types.ModuleType("skills")
        pkg.__path__ = [repo_skills]  # type: ignore[attr-defined]
        pkg.__package__ = "skills"
        sys.modules["skills"] = pkg
    else:
        existing = sys.modules["skills"]
        if hasattr(existing, "__path__") and repo_skills not in existing.__path__:
            existing.__path__.insert(0, repo_skills)  # type: ignore[attr-defined]


# Auto-discover skill directory → Python module name mapping.
# Any skill dir with __init__.py gets a skills.<underscored_name> alias
# so the Temporal sandbox can resolve imports during workflow validation.
# No hardcoded consumer list — skills register themselves.
def _build_dir_to_module_map(skills_root: "Path") -> dict[str, str]:
    """Map skill directory names to valid Python module names under ``skills.``."""
    mapping: dict[str, str] = {}
    if not skills_root.is_dir():
        return mapping
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        if not (skill_dir / "__init__.py").exists():
            continue
        mapping[skill_dir.name] = skill_dir.name.replace("-", "_")
    return mapping


def _load_skill_module(skill_dir: "Path", mod_name: str) -> "types.ModuleType | None":
    """Load a skill package from *skill_dir* and register submodules in sys.modules."""
    init_py = skill_dir / "__init__.py"
    if not init_py.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        mod_name, str(init_py),
        submodule_search_locations=[str(skill_dir)],
    )
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pre_register_package_stub(skill_dir: "Path", legacy_name: str) -> None:
    """Register a lightweight ``skills.<legacy_name>`` stub WITHOUT executing code.

    Creates a ``ModuleType`` with ``__path__`` set so Python's import machinery
    can resolve ``from skills.<name>.workflow import ...`` via the stub's
    search path.  The stub is marked with ``_is_stub = True`` so that
    ``_register_legacy_aliases`` knows to replace it with the real module later.

    This must run for ALL skills before any ``exec_module`` calls, so that
    cross-skill imports (e.g. autopilot -> deep_plan) resolve regardless of
    alphabetical directory order.
    """
    _ensure_skills_package()
    pkg_name = f"skills.{legacy_name}"
    if pkg_name in sys.modules:
        return
    stub = types.ModuleType(pkg_name)
    stub.__path__ = [str(skill_dir)]  # type: ignore[attr-defined]
    stub.__package__ = pkg_name
    stub._is_stub = True  # type: ignore[attr-defined]
    sys.modules[pkg_name] = stub


def _register_legacy_aliases(skill_dir: "Path", legacy_name: str) -> None:
    """Register ``skills.<legacy_name>`` and ``skills.<legacy_name>.<sub>`` aliases.

    This lets cross-skill imports like ``from skills.deep_plan.workflow import ...``
    resolve when skills are loaded from the claude-skills directory.

    If a stub was pre-registered by ``_pre_register_package_stub``, it is
    replaced with the real module from ``exec_module``.
    """
    _ensure_skills_package()
    pkg_name = f"skills.{legacy_name}"

    # Register the package (__init__.py) under the legacy name.
    # Replace pre-registered stubs (marked with _is_stub).
    init_py = skill_dir / "__init__.py"
    existing = sys.modules.get(pkg_name)
    is_stub = getattr(existing, "_is_stub", False)
    if init_py.exists() and (existing is None or is_stub):
        spec = importlib.util.spec_from_file_location(
            pkg_name, str(init_py),
            submodule_search_locations=[str(skill_dir)],
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[pkg_name] = mod
            spec.loader.exec_module(mod)

    # Register each .py submodule (workflow.py, state.py, activities.py, etc.)
    for py_file in sorted(skill_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        sub_name = py_file.stem
        full_name = f"{pkg_name}.{sub_name}"
        if full_name in sys.modules:
            continue
        sub_spec = importlib.util.spec_from_file_location(full_name, str(py_file))
        if sub_spec and sub_spec.loader:
            sub_mod = importlib.util.module_from_spec(sub_spec)
            sys.modules[full_name] = sub_mod
            sub_spec.loader.exec_module(sub_mod)


def build_registry() -> SkillRegistry:
    """Dynamically discover and register every skill from the claude-skills dir.

    Scans ``~/.claude/skills/`` (overridable via ``CLAUDE_SKILLS_DIR``) for
    directories containing an ``__init__.py`` with a ``register(registry)``
    function.  Each such module is loaded via ``importlib`` and registered.

    Also populates ``sys.modules`` under the legacy ``skills.<name>`` paths so
    cross-skill workflow imports (autopilot -> deep_plan, etc.) resolve.

    The generic interpreter (``skills/generic/``) is always registered as a
    fallback from the sagaflow repo, not the claude-skills dir.

    Loading uses three phases to handle cross-skill dependencies (e.g.
    autopilot imports deep_plan, team, etc.):

    Phase 1 — Stub pre-registration: lightweight ``ModuleType`` stubs with
    ``__path__`` set for every legacy skill.  No code is executed.

    Phase 2 — Legacy alias registration: ``exec_module`` runs each skill's
    ``__init__.py`` and submodules.  Cross-skill imports now resolve via the
    stubs from Phase 1 regardless of alphabetical directory order.

    Phase 3 — Skill registration: each skill's ``register(registry)`` is called.
    """
    registry = SkillRegistry()
    failed_skills: dict[str, str] = {}

    skills_root = claude_skills_dir()
    if skills_root.is_dir():
        dir_to_mod = _build_dir_to_module_map(skills_root)

        # Phase 1: pre-register lightweight stubs for ALL skills with
        # __init__.py so cross-skill imports resolve regardless of load order.
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            mod_name = dir_to_mod.get(skill_dir.name)
            if mod_name:
                try:
                    _pre_register_package_stub(skill_dir, mod_name)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("failed to pre-register stub for %s: %s", skill_dir.name, exc)

        # Phase 2: register aliases — now safe because stubs exist.
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            mod_name = dir_to_mod.get(skill_dir.name)
            if mod_name:
                try:
                    _register_legacy_aliases(skill_dir, mod_name)
                except Exception as exc:  # noqa: BLE001
                    failed_skills[skill_dir.name] = str(exc)
                    _log.warning(
                        "failed to register aliases for %s: %s",
                        skill_dir.name, exc, exc_info=True,
                    )

        # Phase 3: register each skill from the already-loaded module.
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            init_py = skill_dir / "__init__.py"
            if not init_py.exists():
                continue
            mod_name = dir_to_mod.get(skill_dir.name)
            mod = None
            if mod_name:
                mod = sys.modules.get(f"skills.{mod_name}")
                if getattr(mod, "_is_stub", False):
                    mod = None
            if mod is None and mod_name:
                mod = _load_skill_module(skill_dir, f"skills.{mod_name}")
            try:
                if mod and hasattr(mod, "register"):
                    mod.register(registry)
            except Exception as exc:  # noqa: BLE001
                failed_skills[skill_dir.name] = str(exc)
                _log.warning(
                    "failed to register skill %s: %s",
                    skill_dir.name, exc, exc_info=True,
                )

    # Generic interpreter: runs any claude-skills SKILL.md via Claude tool-use.
    # Lives in sagaflow repo (skills/generic/), not in claude-skills dir.
    try:
        from skills import generic as generic_skill
        generic_skill.register(registry)
    except ImportError:
        pass

    # Manifest-driven skills: scan for SKILL.md files with execution: frontmatter.
    # These use the shared ManifestWorkflow instead of per-skill workflow.py.
    _register_manifested_skills(registry, skills_root)

    # @workflow-decorated functions: scan workflow.py files for sagaflow.api.workflow
    # decorated functions and register them. Only picks up skills not already registered
    # via __init__.py or manifest.
    _register_api_workflow_skills(registry, skills_root)

    if failed_skills:
        _log.error(
            "skill loading failures (%d): %s",
            len(failed_skills),
            ", ".join(f"{k} ({v})" for k, v in sorted(failed_skills.items())),
        )

    return registry


def _register_manifested_skills(registry: SkillRegistry, skills_root: "Path") -> None:
    """Manifest-based skill registration — disabled pending Pydantic AI migration."""
    pass


def _register_api_workflow_skills(registry: SkillRegistry, skills_root: "Path") -> None:
    """Auto-discover @workflow-decorated functions in workflow.py files.

    Scans skill dirs that have ``workflow.py`` but were NOT already registered
    via ``__init__.py`` or manifest. Loads the module, finds any functions
    decorated with ``@sagaflow.workflow``, and registers them.
    """
    if not skills_root.is_dir():
        return

    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        workflow_py = skill_dir / "workflow.py"
        if not workflow_py.exists():
            continue
        if skill_dir.name in registry.names():
            continue
        if (skill_dir / "__init__.py").exists():
            continue

        try:
            spec = importlib.util.spec_from_file_location(
                f"_sagaflow_api_skill_{skill_dir.name.replace('-', '_')}",
                str(workflow_py),
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        except Exception as exc:
            _log.debug("skipping %s for api-workflow discovery: %s", skill_dir.name, exc)
            continue

    try:
        from sagaflow.api import register_api_workflows, _WORKFLOW_REGISTRY
        if _WORKFLOW_REGISTRY:
            register_api_workflows(registry)
            for name in _WORKFLOW_REGISTRY:
                if name in registry.names():
                    _log.info("registered api-workflow skill: %s", name)
    except ImportError:
        pass


def build_extra_workflows() -> list:
    """Workflow classes beyond the 1-per-skill set tracked by SkillRegistry.

    ``SubagentWorkflow`` is dispatched as a child by ``ClaudeSkillWorkflow`` but
    doesn't correspond to a registered skill. Temporal workers must list every
    workflow class in ``workflows=``, so we collect these here.

    Mission workflows (MissionWorkflow + observer children) are also registered
    here since they don't correspond to a skill in the SkillRegistry.
    """
    extras: list = []
    try:
        from skills import generic as generic_skill
        extras.extend(generic_skill.extra_workflows())
    except ImportError:
        pass

    # Mission workflows — absorbed from swarmd.
    try:
        from sagaflow.missions.workflow import MissionWorkflow
        from sagaflow.missions.specialists import (
            LLMCriticWorkflow,
            PatternDetectorWorkflow,
            ResourceMonitorWorkflow,
        )
        extras.extend([
            MissionWorkflow,
            PatternDetectorWorkflow,
            LLMCriticWorkflow,
            ResourceMonitorWorkflow,
        ])
    except ImportError:
        pass

    try:
        from sagaflow.durable.chain_workflow import ChainWorkflow
        extras.append(ChainWorkflow)
    except ImportError:
        pass

    try:
        from sagaflow.api import ApiWorkflow
        extras.append(ApiWorkflow)
    except ImportError:
        pass

    try:
        from sagaflow.skill import SkillWorkflow
        extras.append(SkillWorkflow)
    except ImportError:
        pass

    return extras


def build_mission_activities() -> list:
    """Return all mission-specific activity functions for worker registration."""
    activities: list = []
    try:
        from sagaflow.missions.activities import (
            check_criterion,
            check_disk_activity,
            check_memory_activity,
            check_zombies_activity,
            completion_judge_activity,
            detect_scope_shrinking_activity,
            emit_finding_activity,
            enforce_invariants,
            goal_drift_check_activity,
            intervention_judge_activity,
            is_agent_quiescent_activity,
            progress_audit_activity,
            read_recent_events_activity,
            restart_subprocess_activity,
            run_anticheat_dimension_activity,
            run_claude_cli_activity,
            spawn_subagent_activity,
            verify_tamper,
        )
        activities.extend([
            check_criterion,
            check_disk_activity,
            check_memory_activity,
            check_zombies_activity,
            completion_judge_activity,
            detect_scope_shrinking_activity,
            emit_finding_activity,
            enforce_invariants,
            goal_drift_check_activity,
            intervention_judge_activity,
            is_agent_quiescent_activity,
            progress_audit_activity,
            read_recent_events_activity,
            restart_subprocess_activity,
            run_anticheat_dimension_activity,
            run_claude_cli_activity,
            spawn_subagent_activity,
            verify_tamper,
        ])
    except ImportError:
        pass
    return activities


from contextlib import suppress as _suppress


_TERMINAL_STATUSES = frozenset({
    "WORKFLOW_EXECUTION_STATUS_FAILED",
    "WORKFLOW_EXECUTION_STATUS_TIMED_OUT",
    "WORKFLOW_EXECUTION_STATUS_TERMINATED",
    "WORKFLOW_EXECUTION_STATUS_CANCELED",
})


async def _check_orphaned_terminals(client: Client) -> None:
    """Detect Temporal workflows in terminal state whose manifest wasn't finalized.

    When a workflow dies from infrastructure failure (worker crash, Temporal
    timeout, SQLite wedge), the in-workflow ``finalize()`` never runs — the
    manifest stays RUNNING and no inbox entry is emitted. This function
    catches those orphans.
    """
    from sagaflow.inbox import Inbox, InboxEntry
    from sagaflow.notify import notify_desktop
    from sagaflow.run_manifest import _manifest_path, _read_manifest, finalize_manifest

    paths = Paths.from_env()
    inbox = Inbox(path=paths.inbox)

    async for wf in client.list_workflows(f"TaskQueue = '{TASK_QUEUE}'"):
        status_name = wf.status.name if wf.status else None
        if status_name not in ("FAILED", "TIMED_OUT", "TERMINATED", "CANCELED"):
            continue

        run_id = wf.id.removeprefix("sagaflow-")
        run_dir = paths.run_dir_for(run_id)
        manifest_file = _manifest_path(run_dir)
        if not manifest_file.exists():
            continue

        try:
            data = _read_manifest(run_dir)
        except Exception:
            continue
        if data.get("status") != "RUNNING":
            continue

        skill = data.get("skill", "unknown")
        _log.warning(
            "orphaned terminal: %s is %s in Temporal but RUNNING in manifest (skill=%s)",
            run_id, status_name, skill,
        )
        try:
            finalize_manifest(
                run_dir,
                status=status_name,
                termination={"reason": f"health_monitor: Temporal status {status_name}, finalize() never ran"},
            )
            inbox.append(InboxEntry(
                run_id=run_id,
                skill=skill,
                status=status_name,
                summary=f"died without finalize: Temporal marked {status_name}",
                timestamp=datetime.now(timezone.utc),
            ))
            notify_desktop(
                title=f"sagaflow: {run_id} {status_name}",
                body=f"{skill}: died without finalize",
            )
        except Exception as exc:
            _log.warning("failed to finalize orphaned run %s: %s", run_id, exc)


async def _is_worker_reachable(client: Client) -> bool:
    try:
        resp = await client.service_client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(namespace=DEFAULT_NAMESPACE, task_queue=TaskQueue(name=TASK_QUEUE))
        )
    except Exception:  # noqa: BLE001
        return False
    return len(resp.pollers) > 0


def _s6_worker_managed() -> bool:
    """Return True if an s6 service is managing the sagaflow worker."""
    return Path("/var/run/s6/services/USER_sagaflow_worker/run").exists()


async def ensure_worker_running(*, target: str = DEFAULT_TARGET) -> None:
    """If no worker is polling the queue, fork `sagaflow worker run --detached-child`.

    When an s6 service (USER_sagaflow_worker) is managing the worker, skip
    auto-spawn — s6 handles restarts and spawning a second worker would
    double resource usage.

    Uses a file lock so concurrent ``sagaflow launch`` invocations don't
    race to spawn multiple workers. The first caller acquires the lock,
    checks reachability, spawns if needed, and releases. Concurrent
    callers block on the lock and re-check reachability after acquiring
    (the first caller's worker is likely already polling by then).
    """
    import fcntl

    client = await connect(target=target)
    if await _is_worker_reachable(client):
        return

    if _s6_worker_managed():
        _log.info("s6 manages USER_sagaflow_worker; waiting for it instead of auto-spawning")
        for _ in range(30):
            await asyncio.sleep(1.0)
            if await _is_worker_reachable(client):
                return
        raise RuntimeError("s6-managed worker did not become reachable within 30s")

    lock_path = Paths.from_env().root / ".worker-spawn.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if await _is_worker_reachable(client):
            return
        log_dir = Paths.from_env().worker_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"worker-{os.getpid()}.log"
        subprocess.Popen(
            [sys.executable, "-m", "sagaflow.cli", "worker", "run", "--detached-child"],
            stdout=log_file.open("ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(30):
            await asyncio.sleep(0.5)
            if await _is_worker_reachable(client):
                return
        raise RuntimeError("auto-spawned worker did not become ready within 15s")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


async def run_worker(*, target: str = DEFAULT_TARGET) -> None:
    """Foreground worker. Blocks until process killed."""

    _configure_worker_logging()
    _log.info("sagaflow worker starting (PID %d)", os.getpid())

    client = await connect(target=target)
    registry = build_registry()
    workflows = list(registry.all_workflows())
    # Seen-set lets us dedupe while preserving order (some extras may already
    # be registered via a skill).
    seen = {id(w) for w in workflows}
    for extra in build_extra_workflows():
        if id(extra) in seen:
            continue
        seen.add(id(extra))
        workflows.append(extra)
    # Dedupe activities by Temporal name — skills and missions share some
    # (emit_finding, spawn_subagent, etc.) and Temporal rejects duplicates.
    combined = list(registry.all_activities()) + build_mission_activities()
    from sagaflow.slack_progress import (
        report_slack_progress as _slack_progress_act,
        deliver_artifact_to_slack as _deliver_artifact_act,
        report_slack_failure as _slack_failure_act,
        report_slack_state_change as _state_change_act,
    )
    from sagaflow.durable.activities import finalize_manifest_activity as _finalize_act
    from sagaflow.durable.activities import run_shell_activity as _run_shell_act
    combined.extend([_slack_progress_act, _deliver_artifact_act, _slack_failure_act, _state_change_act, _finalize_act, _run_shell_act])
    from sagaflow.memory.activities import commit_outcome as _commit_outcome_act, recall_outcomes as _recall_outcomes_act
    combined.extend([_commit_outcome_act, _recall_outcomes_act])
    from sagaflow.durable.activities import (
        record_sdk_telemetry as _record_sdk_act,
        budget_pre_dispatch as _budget_pre_act,
    )
    combined.extend([_record_sdk_act, _budget_pre_act])
    try:
        from sagaflow.durable.chain_workflow import (
            load_chain_declaration,
            resolve_skill_content,
            write_chain_file,
            read_step_termination,
            check_chain_exports,
        )
        combined.extend([load_chain_declaration, resolve_skill_content, write_chain_file, read_step_termination, check_chain_exports])
    except ImportError:
        pass
    seen_act: set[str] = set()
    all_activities: list = []
    for act in combined:
        defn = getattr(act, "__temporal_activity_definition", None)
        key = defn.name if defn is not None else f"fn:{id(act)}"
        if key not in seen_act:
            seen_act.add(key)
            all_activities.append(act)
    _max_activities = int(os.environ.get(
        "SAGAFLOW_MAX_CONCURRENT_ACTIVITIES",
        str(_default_max_activities()),
    ))
    _max_wf_tasks = int(os.environ.get(
        "SAGAFLOW_MAX_CONCURRENT_WORKFLOW_TASKS",
        str(max(2, _max_activities // 2)),
    ))
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=workflows,
        activities=all_activities,
        plugins=[PydanticAIPlugin()],
        workflow_runner=_build_sandbox_runner(),
        max_concurrent_activities=_max_activities,
        max_concurrent_workflow_tasks=_max_wf_tasks,
        debug_mode=True,
    )
    try:
        from sagaflow.run_manifest import cleanup_stale_runs
        cleaned = cleanup_stale_runs()
        if cleaned:
            _log.info('cleaned up %d stale runs on startup', cleaned)
    except ImportError:
        pass

    _log.info(
        "worker ready: %d workflows, %d activities, max_activities=%d, max_wf_tasks=%d",
        len(workflows), len(all_activities), _max_activities, _max_wf_tasks,
    )

    async def _run_health_monitor() -> None:
        """Background loop: detect workflows that died without finalize()."""
        await asyncio.sleep(30)
        while True:
            try:
                await _check_orphaned_terminals(client)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                _log.warning("health monitor tick FAILED — cannot verify workflow health", exc_info=True)
            await asyncio.sleep(60)

    monitor_task = asyncio.create_task(_run_health_monitor())
    try:
        await worker.run()
    except RuntimeError as exc:
        if "interrupted" in str(exc).lower():
            _log.error(
                "Temporal server returned 'interrupted' — likely wedged from "
                "rapid worker restarts. Attempting auto-recovery by restarting "
                "the Temporal server.",
            )
            if _restart_temporal_server():
                _log.info("Temporal server restarted. Re-run the worker.")
            else:
                _log.error("Could not auto-restart Temporal server.")
        _log.critical("worker crashed fatally", exc_info=True)
        raise
    except Exception:
        _log.critical("worker crashed fatally", exc_info=True)
        raise
    finally:
        monitor_task.cancel()
        with _suppress(asyncio.CancelledError):
            await monitor_task
        _log.info("worker shutting down (PID %d)", os.getpid())


def _restart_temporal_server() -> bool:
    """Find and restart the Temporal dev server process."""
    import signal
    import time

    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "temporal server start-dev"], text=True,
        )
        pids = [int(p) for p in out.strip().split("\n") if p]
    except (subprocess.CalledProcessError, ValueError):
        _log.warning("No Temporal server process found to restart")
        return False

    cmd_line = None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                cmd_line = f.read().replace("\x00", " ").strip()
                break
        except OSError:
            continue

    for pid in pids:
        _log.info("Sending SIGTERM to Temporal server PID %d", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        alive = any(_pid_exists(p) for p in pids)
        if not alive:
            break
        time.sleep(0.5)

    if cmd_line:
        import shlex
        args = shlex.split(cmd_line)
        _log.info("Restarting Temporal server: %s", " ".join(args))
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(3)
        return True
    return False


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
