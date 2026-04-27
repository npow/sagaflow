"""Behavioral diff engine for sagaflow runs.

Pure functions over ``RunManifest`` dataclasses. No I/O except reading
the two manifest files via ``read_manifest()``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sagaflow.manifest import RunManifest, read_manifest

logger = logging.getLogger(__name__)

VERDICT_INCOMPARABLE = "INCOMPARABLE"
VERDICT_REGRESSION = "REGRESSION"
VERDICT_IMPROVEMENT = "IMPROVEMENT"
VERDICT_BEHAVIORAL_CHANGE = "BEHAVIORAL_CHANGE"
VERDICT_COSMETIC_CHANGE = "COSMETIC_CHANGE"
VERDICT_IDENTICAL = "IDENTICAL"

_NUMERIC_TOLERANCE = 0.20


@dataclass
class SignalDiff:
    key: str
    value_a: Any
    value_b: Any
    delta: Any
    classification: str  # "equal", "regression", "improvement", "change", "incomparable"


@dataclass
class CostDiff:
    duration_a: float | None
    duration_b: float | None
    tokens_a: int
    tokens_b: int
    cost_a: float
    cost_b: float


@dataclass
class ArtifactDiff:
    only_in_a: list[str]
    only_in_b: list[str]
    size_changes: dict[str, tuple[int, int]]


@dataclass
class StepDiff:
    count_a: int
    count_b: int
    role_diff: list[str]


@dataclass
class ComparisonResult:
    verdict: str
    run_a: RunManifest
    run_b: RunManifest
    same_input: bool
    code_changed: bool
    termination_diff: str | None
    signal_diffs: list[SignalDiff]
    cost_diff: CostDiff
    artifact_diff: ArtifactDiff
    step_diff: StepDiff
    warnings: list[str] = field(default_factory=list)


def compare_runs(run_a: str | RunManifest, run_b: str | RunManifest) -> ComparisonResult:
    a = run_a if isinstance(run_a, RunManifest) else read_manifest(run_a)
    b = run_b if isinstance(run_b, RunManifest) else read_manifest(run_b)

    warnings: list[str] = []
    if a.backfilled:
        warnings.append(f"Run A ({a.run_id}) is a backfilled manifest")
    if b.backfilled:
        warnings.append(f"Run B ({b.run_id}) is a backfilled manifest")

    if a.skill != b.skill:
        return _incomparable_result(a, b, warnings)

    same_input = _check_same_input(a, b)
    code_changed = _check_code_changed(a, b)
    termination_diff = _diff_termination(a, b)
    signal_diffs = _diff_signals(a, b)
    cost_diff = _diff_cost(a, b)
    artifact_diff = _diff_artifacts(a, b)
    step_diff = _diff_steps(a, b)

    verdict = _classify_verdict(a, b, termination_diff, signal_diffs)

    return ComparisonResult(
        verdict=verdict,
        run_a=a,
        run_b=b,
        same_input=same_input,
        code_changed=code_changed,
        termination_diff=termination_diff,
        signal_diffs=signal_diffs,
        cost_diff=cost_diff,
        artifact_diff=artifact_diff,
        step_diff=step_diff,
        warnings=warnings,
    )


def _incomparable_result(a: RunManifest, b: RunManifest, warnings: list[str]) -> ComparisonResult:
    return ComparisonResult(
        verdict=VERDICT_INCOMPARABLE,
        run_a=a, run_b=b,
        same_input=False, code_changed=False,
        termination_diff="different skills",
        signal_diffs=[], cost_diff=CostDiff(None, None, 0, 0, 0.0, 0.0),
        artifact_diff=ArtifactDiff([], [], {}),
        step_diff=StepDiff(0, 0, []),
        warnings=warnings,
    )


def _check_same_input(a: RunManifest, b: RunManifest) -> bool:
    h_a = (a.input or {}).get("input_hash")
    h_b = (b.input or {}).get("input_hash")
    if h_a is None or h_b is None:
        return False
    return h_a == h_b


def _check_code_changed(a: RunManifest, b: RunManifest) -> bool:
    wh_a = (a.skill_version or {}).get("workflow_content_hash")
    wh_b = (b.skill_version or {}).get("workflow_content_hash")
    if wh_a is None or wh_b is None:
        return False
    return wh_a != wh_b


def _diff_termination(a: RunManifest, b: RunManifest) -> str | None:
    label_a = (a.termination or {}).get("label")
    label_b = (b.termination or {}).get("label")
    if label_a == label_b:
        return None
    return f"{label_a} → {label_b}"


def _diff_signals(a: RunManifest, b: RunManifest) -> list[SignalDiff]:
    sig_a = (a.behavioral_signals or {}).get("raw", {})
    sig_b = (b.behavioral_signals or {}).get("raw", {})
    src_a = (a.behavioral_signals or {}).get("source", "missing")
    src_b = (b.behavioral_signals or {}).get("source", "missing")

    if src_a == "missing" or src_b == "missing":
        return [SignalDiff(
            key="_source", value_a=src_a, value_b=src_b,
            delta=None, classification="incomparable",
        )]

    all_keys = sorted(set(list(sig_a.keys()) + list(sig_b.keys())))
    diffs: list[SignalDiff] = []
    for key in all_keys:
        va = sig_a.get(key)
        vb = sig_b.get(key)
        classification, delta = _classify_signal(key, va, vb)
        diffs.append(SignalDiff(
            key=key, value_a=va, value_b=vb,
            delta=delta, classification=classification,
        ))
    return diffs


def _classify_signal(key: str, va: Any, vb: Any) -> tuple[str, Any]:
    if va is None and vb is None:
        return ("equal", None)
    if va is None or vb is None:
        return ("incomparable", None)

    if isinstance(va, bool) and isinstance(vb, bool):
        if va == vb:
            return ("equal", None)
        if key == "coverage_complete":
            if va and not vb:
                return ("regression", "true→false")
            return ("improvement", "false→true")
        return ("change", f"{va}→{vb}")

    if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
        if va == vb:
            return ("equal", 0)
        delta = vb - va

        if key in ("malformed_responses", "quorum_failures"):
            if delta > 0:
                return ("regression", delta)
            if delta < 0:
                return ("improvement", delta)
            return ("equal", 0)

        if va != 0:
            pct = abs(delta) / abs(va)
        else:
            pct = 1.0 if delta != 0 else 0.0

        if pct <= _NUMERIC_TOLERANCE:
            return ("equal", delta)

        if key == "defect_count":
            if delta > 0:
                return ("regression", delta)
            return ("improvement", delta)

        return ("change", delta)

    if va == vb:
        return ("equal", None)
    return ("change", f"{va}→{vb}")


def _diff_cost(a: RunManifest, b: RunManifest) -> CostDiff:
    ca = a.cost or {}
    cb = b.cost or {}
    return CostDiff(
        duration_a=(a.timing or {}).get("duration_seconds"),
        duration_b=(b.timing or {}).get("duration_seconds"),
        tokens_a=(ca.get("total_input_tokens") or 0) + (ca.get("total_output_tokens") or 0),
        tokens_b=(cb.get("total_input_tokens") or 0) + (cb.get("total_output_tokens") or 0),
        cost_a=ca.get("estimated_cost_usd") or 0.0,
        cost_b=cb.get("estimated_cost_usd") or 0.0,
    )


def _diff_artifacts(a: RunManifest, b: RunManifest) -> ArtifactDiff:
    names_a = {art["name"]: art.get("size_bytes", 0) for art in (a.artifacts or [])}
    names_b = {art["name"]: art.get("size_bytes", 0) for art in (b.artifacts or [])}
    only_a = sorted(set(names_a) - set(names_b))
    only_b = sorted(set(names_b) - set(names_a))
    size_changes = {}
    for name in sorted(set(names_a) & set(names_b)):
        if names_a[name] != names_b[name]:
            size_changes[name] = (names_a[name], names_b[name])
    return ArtifactDiff(only_in_a=only_a, only_in_b=only_b, size_changes=size_changes)


def _diff_steps(a: RunManifest, b: RunManifest) -> StepDiff:
    steps_a = a.steps or []
    steps_b = b.steps or []
    roles_a = [s.get("role", "") for s in steps_a]
    roles_b = [s.get("role", "") for s in steps_b]
    role_diff = []
    if roles_a != roles_b:
        only_a = set(roles_a) - set(roles_b)
        only_b = set(roles_b) - set(roles_a)
        if only_a:
            role_diff.append(f"only in A: {sorted(only_a)}")
        if only_b:
            role_diff.append(f"only in B: {sorted(only_b)}")
    return StepDiff(count_a=len(steps_a), count_b=len(steps_b), role_diff=role_diff)


def _classify_verdict(
    a: RunManifest, b: RunManifest,
    termination_diff: str | None,
    signal_diffs: list[SignalDiff],
) -> str:
    src_a = (a.behavioral_signals or {}).get("source", "missing")
    src_b = (b.behavioral_signals or {}).get("source", "missing")
    if src_a == "missing" or src_b == "missing":
        return VERDICT_INCOMPARABLE

    has_regression = any(d.classification == "regression" for d in signal_diffs)
    has_improvement = any(d.classification == "improvement" for d in signal_diffs)
    has_change = any(d.classification == "change" for d in signal_diffs)

    if has_regression:
        return VERDICT_REGRESSION
    if has_improvement and not has_change:
        return VERDICT_IMPROVEMENT
    if termination_diff or has_change or has_improvement:
        return VERDICT_BEHAVIORAL_CHANGE

    cost_a = (a.cost or {}).get("estimated_cost_usd", 0.0)
    cost_b = (b.cost or {}).get("estimated_cost_usd", 0.0)
    steps_a = len(a.steps or [])
    steps_b = len(b.steps or [])
    dur_a = (a.timing or {}).get("duration_seconds")
    dur_b = (b.timing or {}).get("duration_seconds")
    if cost_a != cost_b or steps_a != steps_b or dur_a != dur_b:
        return VERDICT_COSMETIC_CHANGE

    return VERDICT_IDENTICAL


def format_comparison(result: ComparisonResult, fmt: str = "text") -> str:
    if fmt == "json":
        return _format_json(result)
    if fmt == "markdown":
        return _format_markdown(result)
    return _format_text(result)


def _format_text(r: ComparisonResult) -> str:
    lines: list[str] = []
    lines.append("═══ Behavior Comparison ═══════════════════════════════════════")
    lines.append("")
    lines.append(f"Run A: {r.run_a.run_id}")
    lines.append(f"Run B: {r.run_b.run_id}")
    lines.append(f"Skill: {r.run_a.skill}")
    ih_a = (r.run_a.input or {}).get("input_hash", "?")
    lines.append(f"Same input: {'yes' if r.same_input else 'no'} ({ih_a})")
    lines.append(f"Code changed: {'yes' if r.code_changed else 'no'}")
    if r.warnings:
        for w in r.warnings:
            lines.append(f"⚠ {w}")

    lines.append("")
    lines.append("─── Termination ──────────────────────────────────────────────")
    label_a = (r.run_a.termination or {}).get("label", "?")
    label_b = (r.run_b.termination or {}).get("label", "?")
    lines.append(f"A: {label_a}")
    lines.append(f"B: {label_b}")
    if r.termination_diff:
        lines.append(f"△: {r.termination_diff}")

    lines.append("")
    lines.append("─── Behavioral Signals ───────────────────────────────────────")
    for sd in r.signal_diffs:
        if sd.key.startswith("_"):
            continue
        flag = ""
        if sd.classification == "regression":
            flag = " ▼ REGRESSION"
        elif sd.classification == "improvement":
            flag = " ▲ IMPROVEMENT"
        elif sd.classification == "change":
            flag = " △"
        elif sd.classification == "incomparable":
            flag = " ?"
        lines.append(f"  {sd.key:30s}  {_fmt_val(sd.value_a):>10s}  {_fmt_val(sd.value_b):>10s}  {_fmt_delta(sd.delta):>10s}{flag}")

    lines.append("")
    lines.append("─── Cost & Performance ───────────────────────────────────────")
    cd = r.cost_diff
    lines.append(f"  duration  {_fmt_duration(cd.duration_a):>12s}  {_fmt_duration(cd.duration_b):>12s}")
    lines.append(f"  tokens    {cd.tokens_a:>12,}  {cd.tokens_b:>12,}")
    lines.append(f"  cost      ${cd.cost_a:>11.2f}  ${cd.cost_b:>11.2f}")

    lines.append("")
    lines.append("═══════════════════════════════════════════════════════════════")
    lines.append(f"Verdict: {r.verdict}")
    lines.append("═══════════════════════════════════════════════════════════════")
    return "\n".join(lines)


def _format_json(r: ComparisonResult) -> str:
    data = {
        "verdict": r.verdict,
        "run_a": r.run_a.run_id,
        "run_b": r.run_b.run_id,
        "skill": r.run_a.skill,
        "same_input": r.same_input,
        "code_changed": r.code_changed,
        "termination_diff": r.termination_diff,
        "signals": [
            {"key": sd.key, "a": sd.value_a, "b": sd.value_b,
             "delta": sd.delta, "classification": sd.classification}
            for sd in r.signal_diffs
        ],
        "cost": {
            "duration_a": r.cost_diff.duration_a,
            "duration_b": r.cost_diff.duration_b,
            "cost_a": r.cost_diff.cost_a,
            "cost_b": r.cost_diff.cost_b,
        },
        "warnings": r.warnings,
    }
    return json.dumps(data, indent=2, default=str)


def _format_markdown(r: ComparisonResult) -> str:
    return _format_text(r)


def _fmt_val(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _fmt_delta(d: Any) -> str:
    if d is None:
        return ""
    if isinstance(d, (int, float)):
        sign = "+" if d > 0 else ""
        return f"{sign}{d}"
    return str(d)


def _fmt_duration(secs: float | None) -> str:
    if secs is None:
        return "—"
    m, s = divmod(int(secs), 60)
    return f"{m}m {s:02d}s"
