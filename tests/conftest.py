"""Test-suite conftest: wire up skill imports from the claude-skills directory.

After the repo consolidation, skill code lives in ``~/.claude/skills/<name>-temporal/``
instead of ``skills/<underscore_name>/``. Tests still import
``from skills.<name>.workflow import ...``.  This conftest dynamically loads the
modules from the claude-skills dir and injects them into ``sys.modules`` so old
import paths keep working without touching every test file.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CI shim: replace temporalio with lightweight stubs so the Rust/tokio runtime
# never starts.  GitHub Actions detects tokio background threads as orphan
# processes and kills the job mid-run.
# ---------------------------------------------------------------------------
if os.environ.get("CI"):
    import types

    class _Noop:
        """Stand-in for decorators, dataclasses, context managers, and attribute bags."""

        def __init__(self, **kw: object) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

        def __call__(self, *args: object, **kw: object) -> object:  # type: ignore[assignment]
            # Accept *args so the stub doesn't reject calls like
            # `payload_converter.from_payloads(payloads, [inp_type])` which
            # use multiple positional arguments. Behavior is unchanged for the
            # decorator/factory case (single callable as `args[0]`).
            fn = args[0] if args else None
            if fn is not None and callable(fn):
                defn = _Noop(**{**vars(self), **kw})
                fn.__temporal_activity_definition = defn  # type: ignore[attr-defined]
                fn.__temporal_workflow_definition = defn  # type: ignore[attr-defined]
                return fn
            return _Noop(**kw)

        def __getattr__(self, name: str) -> "_Noop":
            return _Noop()

        def __enter__(self) -> "_Noop":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    class _TemporalStub(types.ModuleType):
        """Auto-vivifying module stub that satisfies any temporalio import."""

        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.__path__: list[str] = []
            self.__package__ = name

        def __call__(self, fn: object = None, **kw: object) -> object:  # type: ignore[override]
            if fn is not None and callable(fn):
                defn = _Noop(name=getattr(fn, "__name__", ""))
                fn.__temporal_activity_definition = defn  # type: ignore[attr-defined]
                fn.__temporal_workflow_definition = defn  # type: ignore[attr-defined]
                return fn
            return _Noop(**kw)

        def __getattr__(self, name: str) -> "_TemporalStub":
            child = _TemporalStub(f"{self.__name__}.{name}")
            setattr(self, name, child)
            return child

    class _TemporalFinder:
        """Meta-path finder that intercepts all ``temporalio.*`` imports."""

        @staticmethod
        def find_module(name: str, path: object = None) -> "_TemporalFinder | None":
            if name == "temporalio" or name.startswith("temporalio."):
                return _TemporalFinder()
            return None

        @staticmethod
        def load_module(name: str) -> types.ModuleType:
            if name in sys.modules:
                return sys.modules[name]
            mod = _TemporalStub(name)
            sys.modules[name] = mod
            return mod

    sys.meta_path.insert(0, _TemporalFinder())

from sagaflow.prompts import claude_skills_dir

def _discover_skill_map() -> dict[str, str]:
    """Auto-discover skill dirs with __init__.py → {python_name: dir_name}."""
    root = claude_skills_dir()
    if not root.is_dir():
        return {}
    mapping: dict[str, str] = {}
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir():
            continue
        if not (skill_dir / "__init__.py").exists():
            continue
        mapping[skill_dir.name.replace("-", "_")] = skill_dir.name
    return mapping

_SKILL_MAP = _discover_skill_map()


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
    """Populate ``sys.modules`` so ``from skills.<name>.<submod>`` resolves.

    Uses three-phase loading (matching worker.py) to handle cross-skill
    imports like autopilot -> deep_plan.
    """
    root = claude_skills_dir()

    repo_skills_dir = str(Path(__file__).resolve().parent.parent / "skills")
    if "skills" not in sys.modules:
        import types
        skills_pkg = types.ModuleType("skills")
        skills_pkg.__path__ = [repo_skills_dir]
        skills_pkg.__package__ = "skills"
        sys.modules["skills"] = skills_pkg
    else:
        existing = sys.modules["skills"]
        if hasattr(existing, "__path__") and repo_skills_dir not in existing.__path__:
            existing.__path__.insert(0, repo_skills_dir)

    # Phase 1: pre-register stubs so cross-skill imports resolve.
    for old_name, dir_name in _SKILL_MAP.items():
        skill_dir = root / dir_name
        if not skill_dir.is_dir():
            continue
        pkg_name = f"skills.{old_name}"
        if pkg_name not in sys.modules:
            import types
            stub = types.ModuleType(pkg_name)
            stub.__path__ = [str(skill_dir)]
            stub.__package__ = pkg_name
            stub._is_stub = True
            sys.modules[pkg_name] = stub

    # Phase 2: load real modules (stubs allow cross-skill imports to resolve).
    for old_name, dir_name in _SKILL_MAP.items():
        skill_dir = root / dir_name
        if not skill_dir.is_dir():
            continue

        pkg_name = f"skills.{old_name}"

        init_py = skill_dir / "__init__.py"
        if init_py.exists():
            existing = sys.modules.get(pkg_name)
            if existing is None or getattr(existing, "_is_stub", False):
                spec = importlib.util.spec_from_file_location(
                    pkg_name, str(init_py),
                    submodule_search_locations=[str(skill_dir)],
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[pkg_name] = mod
                    spec.loader.exec_module(mod)

        for py_file in sorted(skill_dir.glob("*.py")):
            if py_file.name == "__init__.py":
                continue
            submod_name = py_file.stem
            full_name = f"{pkg_name}.{submod_name}"
            _load_module_from_file(full_name, py_file)


# Only inject when the claude-skills directory actually exists and contains
# skill subdirs.  CI runs unit tests only (--ignore=tests/{generic,scenarios,skills})
# and doesn't need these imports.  Eagerly loading skills pulls in temporalio
# whose Rust core segfaults during Python shutdown on GitHub Actions.
if _SKILL_MAP:
    _inject_skill_modules()
