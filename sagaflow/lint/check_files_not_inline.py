#!/usr/bin/env python3
"""Lint: detect user content inlined into agent prompts via {inp.field} interpolation.

Enforces the files-not-inline contract from _shared/execution-model-contracts.md:
all data passed to agents via files, never inlined into prompts.

Fields on the SAFE_FIELDS allowlist (paths, counts, config) are permitted.
Any other inp.field interpolation is flagged — the author must verify it isn't
user content being force-fed into an agent prompt.

Usage:
    python -m sagaflow.lint.check_files_not_inline [workflow_dir ...]

Exit code 0 = clean, 1 = violations found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SAFE_FIELDS = frozenset({
    "run_dir",
    "run_id",
    "max_rounds",
    "max_agents_per_round",
    "max_depth",
    "hard_stop",
    "hard_cap_usd",
    "max_directions",
    "max_iterations",
    "n_runs",
    "n_stories",
    "artifact_type",
    "artifact_path",
    "seed_path",
    "concept_path",
    "idea_path",
    "task_path",
})

_PATTERN = re.compile(r"\{inp\.(\w+)\}")


def check_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (line_number, field_name, line_text) violations."""
    violations = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        for m in _PATTERN.finditer(line):
            field = m.group(1)
            if field not in SAFE_FIELDS:
                violations.append((i, field, line.strip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if not args:
        print("Usage: python -m sagaflow.lint.check_files_not_inline <dir> [...]", file=sys.stderr)
        return 2

    all_violations: list[tuple[str, int, str, str]] = []
    for d in args:
        for wf in sorted(Path(d).rglob("workflow.py")):
            for lineno, field, text in check_file(wf):
                all_violations.append((str(wf), lineno, field, text))

    if not all_violations:
        print("files-not-inline: OK")
        return 0

    print(f"files-not-inline: {len(all_violations)} potential violation(s)\n")
    for fpath, lineno, field, text in all_violations:
        print(f"  {fpath}:{lineno}  inp.{field}")
        print(f"    {text}\n")
    print(
        "If the field carries user content (seed, concept, task, idea, etc.),\n"
        "write it to a file and reference by path. Add truly safe fields to SAFE_FIELDS."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
