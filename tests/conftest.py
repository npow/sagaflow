"""Test-suite conftest: wire up skill imports from the claude-skills directory.

After the repo consolidation, skill code lives in ``~/.claude/skills/<name>-temporal/``
instead of ``skills/<underscore_name>/``. Tests still import
``from skills.<name>.workflow import ...``.  This conftest dynamically loads the
modules from the claude-skills dir and injects them into ``sys.modules`` so old
import paths keep working without touching every test file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from sagaflow.prompts import claude_skills_dir

# Map: old import package name -> claude-skills directory name
_SKILL_MAP = {
    "hello_world": "hello-world-temporal",
    "deep_qa": "deep-qa-temporal",
    "deep_debug": "deep-debug-temporal",
    "deep_research": "deep-research-temporal",
    "deep_design": "deep-design-temporal",
    "deep_plan": "deep-plan-temporal",
    "proposal_reviewer": "proposal-reviewer-temporal",
    "team": "team-temporal",
    "loop_until_done": "loop-until-done-temporal",
    "flaky_test_diagnoser": "flaky-test-diagnoser-temporal",
    "autopilot": "autopilot-temporal",
}


def _load_module_from_file(mod_name: str, file_path: Path):
    """Load a Python module from *file_path* under *mod_name* in sys.modules."""
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _inject_skill_modules() -> None:
    """Populate ``sys.modules`` so ``from skills.<name>.<submod>`` resolves."""
    root = claude_skills_dir()

    # Ensure the top-level ``skills`` package exists in sys.modules.
    # Include the repo's skills/ directory in __path__ so that
    # ``from skills import generic`` (the in-repo generic interpreter)
    # still works alongside the dynamically-loaded claude-skills modules.
    repo_skills_dir = str(Path(__file__).resolve().parent.parent / "skills")
    if "skills" not in sys.modules:
        import types
        skills_pkg = types.ModuleType("skills")
        skills_pkg.__path__ = [repo_skills_dir]  # type: ignore[attr-defined]
        skills_pkg.__package__ = "skills"
        sys.modules["skills"] = skills_pkg
    else:
        # If skills already exists, ensure the repo path is on __path__
        existing = sys.modules["skills"]
        if hasattr(existing, "__path__") and repo_skills_dir not in existing.__path__:
            existing.__path__.insert(0, repo_skills_dir)

    for old_name, dir_name in _SKILL_MAP.items():
        skill_dir = root / dir_name
        if not skill_dir.is_dir():
            continue

        pkg_name = f"skills.{old_name}"

        # Register the skill package itself (from __init__.py)
        init_py = skill_dir / "__init__.py"
        if init_py.exists():
            _load_module_from_file(pkg_name, init_py)

        # Register each .py submodule (workflow.py, activities.py, state.py, etc.)
        for py_file in sorted(skill_dir.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            submod_name = py_file.stem
            full_name = f"{pkg_name}.{submod_name}"
            _load_module_from_file(full_name, py_file)


# Run at import time so all test modules can use ``from skills.<name>...``.
_inject_skill_modules()
