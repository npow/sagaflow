"""Worker daemon: registers all skills and polls the shared task queue."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
import types
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

_PASSTHROUGH_MODULES = ("httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_", "sniffio")


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


# Map claude-skills directory name -> legacy underscore package name.
# Only entries that have an __init__.py are considered.
_DIR_TO_LEGACY = {
    "hello-world-temporal": "hello_world",
    "deep-qa-temporal": "deep_qa",
    "deep-debug-temporal": "deep_debug",
    "deep-research-temporal": "deep_research",
    "deep-design-temporal": "deep_design",
    "deep-plan-temporal": "deep_plan",
    "proposal-reviewer-temporal": "proposal_reviewer",
    "team-temporal": "team",
    "loop-until-done-temporal": "loop_until_done",
    "flaky-test-diagnoser-temporal": "flaky_test_diagnoser",
    "autopilot-temporal": "autopilot",
}


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
        # Phase 1: pre-register lightweight stubs for ALL legacy skills so
        # that cross-skill imports resolve regardless of load order.
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            legacy = _DIR_TO_LEGACY.get(skill_dir.name)
            if legacy:
                try:
                    _pre_register_package_stub(skill_dir, legacy)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("failed to pre-register stub for %s: %s", skill_dir.name, exc)

        # Phase 2: register legacy aliases — now safe because stubs exist.
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            legacy = _DIR_TO_LEGACY.get(skill_dir.name)
            if legacy:
                try:
                    _register_legacy_aliases(skill_dir, legacy)
                except Exception as exc:  # noqa: BLE001
                    failed_skills[skill_dir.name] = str(exc)
                    _log.warning(
                        "failed to register legacy aliases for %s: %s",
                        skill_dir.name, exc, exc_info=True,
                    )

        # Phase 3: register each skill from the already-loaded legacy module
        # (preferred) or by loading under a `skills.<name>` module name so the
        # Temporal sandbox can resolve it via the passthrough.
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            init_py = skill_dir / "__init__.py"
            if not init_py.exists():
                continue
            legacy = _DIR_TO_LEGACY.get(skill_dir.name)
            mod = None
            if legacy:
                mod = sys.modules.get(f"skills.{legacy}")
                if getattr(mod, "_is_stub", False):
                    mod = None
            if mod is None:
                mod_name = f"skills.{legacy}" if legacy else f"claude_skill_{skill_dir.name.replace('-', '_')}"
                mod = _load_skill_module(skill_dir, mod_name)
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

    if failed_skills:
        _log.error(
            "skill loading failures (%d): %s",
            len(failed_skills),
            ", ".join(f"{k} ({v})" for k, v in sorted(failed_skills.items())),
        )

    return registry


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


async def _is_worker_reachable(client: Client) -> bool:
    try:
        resp = await client.service_client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(namespace=DEFAULT_NAMESPACE, task_queue=TaskQueue(name=TASK_QUEUE))
        )
    except Exception:  # noqa: BLE001
        return False
    return len(resp.pollers) > 0


async def ensure_worker_running(*, target: str = DEFAULT_TARGET) -> None:
    """If no worker is polling the queue, fork `sagaflow worker run --detached-child`.

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

    lock_path = Paths.from_env().root / ".worker-spawn.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check after acquiring lock — another process may have spawned
        # the worker while we were waiting.
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
        # Poll until reachable or timeout.
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
    combined.extend([_slack_progress_act, _deliver_artifact_act, _slack_failure_act, _state_change_act, _finalize_act])
    from sagaflow.memory.activities import commit_outcome as _commit_outcome_act, recall_outcomes as _recall_outcomes_act
    combined.extend([_commit_outcome_act, _recall_outcomes_act])
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
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=workflows,
        activities=all_activities,
        workflow_runner=_build_sandbox_runner(),
        debug_mode=True,
    )
    await worker.run()
