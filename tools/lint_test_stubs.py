#!/usr/bin/env python3
"""Lint that flags tests stubbing the function under test.

Catches the failure mode that shipped two cli bugs in 0.10.22:
a test named ``test_list_prints_running`` patched ``_list_workflows``
with a hardcoded return value, then asserted the printed output looked
right. The reconciliation logic inside ``_list_workflows`` was never
exercised — bugs in that logic could (and did) ship unnoticed.

The rule:
1. In any ``tests/test_<topic>.py`` file
2. For each test function that ``patch``es a symbol from one of the
   sagaflow modules under test (``sagaflow.cli``, ``sagaflow.run_manifest``,
   ``sagaflow.engine``, ``sagaflow.intervention``)
3. If the patched symbol's NAME appears as a substring of the test
   function name, the test is probably stubbing the unit under test.
4. Flag it.

False positive: a test that LEGITIMATELY needs to stub the function
under test (e.g., to test a higher-level CLI command that orchestrates
it) can suppress with ``# noqa: TESTSTUB``.

Exit code 1 on any unsuppressed violation, 0 otherwise.

Usage:
    python tools/lint_test_stubs.py tests/
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Modules whose private/public helpers are commonly the unit under
# test. Tests that stub these are suspect.
SUSPECT_MODULES = (
    "sagaflow.cli",
    "sagaflow.run_manifest",
    "sagaflow.engine",
    "sagaflow.intervention",
    "sagaflow.behavior",
    "sagaflow.notify",
    "sagaflow.inbox",
)

# Infrastructure helpers that tests legitimately stub to avoid running real
# Temporal workers / process spawns / external side effects. Mocking these
# is the *correct* test pattern, not the failure mode this lint targets.
# The lint targets stubbing the LOGIC function under test (e.g.
# _list_workflows in a test that claims to verify the `list` command).
INFRASTRUCTURE_HELPERS = frozenset({
    # CLI setup that touches real systems
    "sagaflow.cli._preflight_all",
    "sagaflow.cli._ensure_hook_installed",
    "sagaflow.cli._ensure_worker_running",
    "sagaflow.cli._start_workflow",
    "sagaflow.cli._await_workflow",
    # Doctor probes are independent and each is its own unit — stubbing
    # one to test the others is normal.
    "sagaflow.cli._probe_temporal",
    "sagaflow.cli._probe_transport",
    "sagaflow.cli._probe_worker",
    "sagaflow.cli._probe_hook",
    "sagaflow.cli._probe_skill_imports",
    "sagaflow.cli._probe_sandbox_lint",
    "sagaflow.cli._probe_payload_safety",
    "sagaflow.cli._probe_stale_runs",
    # Inbox is a state container; stubbing the constructor is mocking state, not logic
    "sagaflow.cli._inbox",
    # Module-level OS-detection constant — patching is the standard way to
    # test platform-conditional code paths
    "sagaflow.notify._PLATFORM",
})


def _extract_patched_targets(node: ast.With) -> list[tuple[str, int]]:
    """Return (target-string, lineno) for every patch(...) / patch.object(...) call in a `with` block."""
    targets: list[tuple[str, int]] = []
    for item in node.items:
        call = item.context_expr
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        # patch("sagaflow.cli._foo", ...)
        if isinstance(func, ast.Name) and func.id == "patch":
            if call.args and isinstance(call.args[0], ast.Constant):
                targets.append((str(call.args[0].value), node.lineno))
        # patch.object(cli, "_foo", ...)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "object"
            and isinstance(func.value, ast.Name)
            and func.value.id == "patch"
        ):
            if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                targets.append(
                    (f"{ast.unparse(call.args[0])}.{call.args[1].value}", node.lineno)
                )
    return targets


def _line_has_suppression(source_lines: list[str], lineno: int) -> bool:
    if lineno <= 0 or lineno > len(source_lines):
        return False
    return "# noqa: TESTSTUB" in source_lines[lineno - 1]


def _matches_suspect_module(target: str) -> str | None:
    """Return the suspect module if target lives in one of them, else None."""
    for mod in SUSPECT_MODULES:
        if target.startswith(mod + ".") or target.startswith(mod + "."):
            return mod
        # patch.object(cli, "_foo") — bare module name from the import alias
        # is hard to verify cheaply, so we accept the common alias names.
        if any(target.startswith(alias + ".") for alias in (mod.rsplit(".", 1)[-1],)):
            return mod
    return None


def _uses_clirunner(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Detect whether the test invokes click's CliRunner.

    CliRunner exercises the full CLI command path, so any private-function
    patch within such a test is high-confidence stubbing-the-unit-under-test:
    the test claims to verify command behavior but bypassed the logic.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "CliRunner":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "invoke":
                return True
    return False


def _check_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{path}: parse error: {exc}"]
    source_lines = source.splitlines()
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        uses_clirunner = _uses_clirunner(node)
        for with_node in ast.walk(node):
            if not isinstance(with_node, ast.With):
                continue
            for target, lineno in _extract_patched_targets(with_node):
                if not _matches_suspect_module(target):
                    continue
                if target in INFRASTRUCTURE_HELPERS:
                    continue
                patched_symbol = target.rsplit(".", 1)[-1].lstrip("_")
                if not patched_symbol:
                    continue
                # Two heuristics, either signals stubbing-the-unit-under-test:
                #   1. patched symbol name appears in the test function name
                #      (e.g. test_finalize_manifest stubs _finalize)
                #   2. test uses click's CliRunner AND patches a private
                #      function from sagaflow.cli — the test claims to
                #      exercise a CLI command but skipped the implementation.
                name_overlap = patched_symbol.lower() in node.name.lower()
                cli_stub = (
                    uses_clirunner
                    and target.startswith("sagaflow.cli.")
                    and target.rsplit(".", 1)[-1].startswith("_")
                )
                if not (name_overlap or cli_stub):
                    continue
                if _line_has_suppression(source_lines, lineno):
                    continue
                reason = "name overlap" if name_overlap else "CliRunner + private cli fn"
                violations.append(
                    f"{path}:{lineno}: test {node.name!r} stubs {target!r} "
                    f"({reason}) — likely stubbing the unit under test "
                    f"(suppress with # noqa: TESTSTUB if intentional)"
                )
    return violations


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]] or [Path("tests")]
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("test_*.py")))
        elif p.is_file():
            files.append(p)
    violations: list[str] = []
    for f in files:
        violations.extend(_check_file(f))
    if violations:
        for v in violations:
            print(v)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
