"""Behavioral signal extraction from run artifacts.

Each skill has an extractor that reads existing artifacts (zero new LLM calls)
and returns a ``BehavioralSignals`` record with structured fields.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)


@dataclass
class BehavioralSignals:
    type: str
    raw: dict[str, Any]
    source: Literal["extracted", "partial", "missing"]


def extract_signals(skill: str, run_dir: Path) -> BehavioralSignals:
    extractor = _EXTRACTORS.get(skill)
    if extractor is None:
        return BehavioralSignals(type=skill, raw={}, source="missing")
    try:
        return extractor(run_dir)
    except Exception as exc:
        logger.error("signal extraction failed for %s in %s: %s", skill, run_dir, exc)
        return BehavioralSignals(type=skill, raw={}, source="missing")


def _read_text(path: Path) -> str | None:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return None


def _count_files(run_dir: Path, pattern: str) -> int:
    return len(list(run_dir.glob(pattern)))


def _extract_deep_qa(run_dir: Path) -> BehavioralSignals:
    report = _read_text(run_dir / "qa-report.md")
    if report is None:
        return BehavioralSignals(type="deep-qa", raw={}, source="missing")

    raw: dict[str, Any] = {}
    m = re.search(r"(\d+)\s+critical\s*/?\s*(\d+)\s+major\s*/?\s*(\d+)\s+minor", report)
    if m:
        crit, maj, minor = int(m.group(1)), int(m.group(2)), int(m.group(3))
        raw["defect_count"] = crit + maj + minor
        raw["by_severity"] = {"critical": crit, "major": maj, "minor": minor}
    else:
        totals = re.search(r"\*\*Totals:\*\*\s*(\d+)\s+critical\s*/\s*(\d+)\s+major\s*/\s*(\d+)\s+minor", report)
        if totals:
            crit, maj, minor = int(totals.group(1)), int(totals.group(2)), int(totals.group(3))
            raw["defect_count"] = crit + maj + minor
            raw["by_severity"] = {"critical": crit, "major": maj, "minor": minor}

    rounds_m = re.search(r"\*\*Rounds:\*\*\s*(\d+)", report)
    if rounds_m:
        raw["rounds_completed"] = int(rounds_m.group(1))

    raw["malformed_responses"] = _count_files(run_dir, "*.malformed_response")
    raw["quorum_failures"] = 0

    categories = set()
    for f in run_dir.glob("critic-r*-*.txt"):
        content = _read_text(f) or ""
        for dim in ["correctness", "completeness", "usability", "security", "operability"]:
            if dim.lower() in content.lower():
                categories.add(dim)
    raw["coverage_categories"] = sorted(categories)
    raw["coverage_explored"] = sorted(categories)
    raw["coverage_complete"] = len(categories) >= 5

    source = "extracted" if "defect_count" in raw else "partial"
    return BehavioralSignals(type="deep-qa", raw=raw, source=source)


def _extract_deep_design(run_dir: Path) -> BehavioralSignals:
    raw: dict[str, Any] = {}
    spec = run_dir / "spec.md"
    if spec.exists():
        raw["spec_size_bytes"] = spec.stat().st_size
    else:
        return BehavioralSignals(type="deep-design", raw={}, source="missing")

    critic_files = sorted(run_dir.glob("critic-r*-*.txt"))
    raw["flaw_count"] = 0
    categories: set[str] = set()
    for cf in critic_files:
        content = _read_text(cf) or ""
        flaws = re.findall(r"(?:FLAW|flaw|Flaw)\b", content)
        raw["flaw_count"] += len(flaws)
        for cat in ["correctness", "usability", "economics", "operability", "security", "outside-frame"]:
            if cat.lower() in content.lower():
                categories.add(cat)

    raw["by_category"] = {c: 0 for c in sorted(categories)}
    rounds = set()
    for cf in critic_files:
        m = re.match(r"critic-r(\d+)-", cf.name)
        if m:
            rounds.add(int(m.group(1)))
    raw["rounds_completed"] = len(rounds)
    raw["coverage_categories"] = sorted(categories)
    raw["coverage_complete"] = len(categories) >= 5
    raw["cross_fix_count"] = _count_files(run_dir, "cross-fix-r*.txt")

    return BehavioralSignals(type="deep-design", raw=raw, source="extracted")


def _extract_deep_debug(run_dir: Path) -> BehavioralSignals:
    report = _read_text(run_dir / "debug-report.md")
    if report is None:
        return BehavioralSignals(type="deep-debug", raw={}, source="missing")

    raw: dict[str, Any] = {}
    hyp_files = list(run_dir.glob("c*-hyp-prompt-*.txt"))
    raw["hypothesis_count"] = len(hyp_files)

    cycles = set()
    for f in run_dir.glob("c*-*"):
        m = re.match(r"c(\d+)-", f.name)
        if m:
            cycles.add(int(m.group(1)))
    raw["cycles"] = len(cycles)

    raw["fix_applied"] = any(run_dir.glob("c*-fix-prompt*.txt"))
    raw["terminal_label"] = None
    label_m = re.search(r"label[=:]\s*(.+?)(?:\n|$)", report, re.IGNORECASE)
    if label_m:
        raw["terminal_label"] = label_m.group(1).strip()

    return BehavioralSignals(type="deep-debug", raw=raw, source="extracted")


def _extract_deep_research(run_dir: Path) -> BehavioralSignals:
    raw: dict[str, Any] = {}
    direction_files = list(run_dir.glob("direction-*.txt")) + list(run_dir.glob("dir-*.txt"))
    raw["direction_count"] = len(direction_files)

    dim_prompt = _read_text(run_dir / "dim-prompt.txt")
    if dim_prompt:
        dims = re.findall(r"(?:DIRECTION|dimension)\s*\d*", dim_prompt, re.IGNORECASE)
        raw["dimension_count"] = max(len(dims), 1)
    else:
        raw["dimension_count"] = 0

    raw["novelty_class"] = "cold_start"
    raw["source_count"] = 0
    raw["convergence_reason"] = None

    report = _read_text(run_dir / "report.md")
    if report:
        src_m = re.findall(r"(?:source|reference|citation)", report, re.IGNORECASE)
        raw["source_count"] = len(src_m)
        source = "extracted"
    else:
        source = "partial" if direction_files else "missing"

    return BehavioralSignals(type="deep-research", raw=raw, source=source)


def _extract_deep_plan(run_dir: Path) -> BehavioralSignals:
    raw: dict[str, Any] = {}

    plan_file = run_dir / "plan.md"
    report = _read_text(plan_file) or _read_text(run_dir / "report.md") or ""

    raw["plan_steps"] = len(re.findall(r"^\s*\d+\.\s", report, re.MULTILINE))
    raw["acceptance_criteria_count"] = len(re.findall(r"(?:AC|acceptance|criteria)", report, re.IGNORECASE))
    raw["architect_verdict"] = None
    raw["critic_verdict"] = None

    for f in run_dir.glob("*.txt"):
        content = _read_text(f) or ""
        if "APPROVE" in content:
            if "architect" in f.name.lower():
                raw["architect_verdict"] = "APPROVE"
            elif "critic" in f.name.lower():
                raw["critic_verdict"] = "APPROVE"
        elif "REJECT" in content:
            if "architect" in f.name.lower():
                raw["architect_verdict"] = "REJECT"
            elif "critic" in f.name.lower():
                raw["critic_verdict"] = "REJECT"

    source = "extracted" if report else "missing"
    return BehavioralSignals(type="deep-plan", raw=raw, source=source)


def _extract_proposal_reviewer(run_dir: Path) -> BehavioralSignals:
    report = _read_text(run_dir / "report.md")
    if report is None:
        return BehavioralSignals(type="proposal-reviewer", raw={}, source="missing")

    raw: dict[str, Any] = {}
    raw["claim_count"] = len(re.findall(r"(?:claim|assertion)", report, re.IGNORECASE))
    raw["weakness_count"] = len(re.findall(r"(?:weakness|concern|risk)", report, re.IGNORECASE))
    raw["credibility_verdict"] = None
    raw["quorum_failures"] = 0

    critic_files = list(run_dir.glob("critic-*.txt"))
    malformed = sum(1 for f in critic_files if not (_read_text(f) or "").strip())
    raw["quorum_failures"] = malformed

    return BehavioralSignals(type="proposal-reviewer", raw=raw, source="extracted")


def _extract_team(run_dir: Path) -> BehavioralSignals:
    report = _read_text(run_dir / "report.md")
    raw: dict[str, Any] = {}

    raw["subtask_count"] = 0
    raw["completed_count"] = 0
    raw["spec_compliance_verdict"] = None
    raw["code_quality_verdict"] = None
    raw["defect_count"] = 0

    if report:
        raw["subtask_count"] = len(re.findall(r"(?:subtask|task)\s*\d", report, re.IGNORECASE))
        source = "extracted"
    else:
        source = "partial" if any(run_dir.glob("*.txt")) else "missing"

    return BehavioralSignals(type="team", raw=raw, source=source)


def _extract_loop_until_done(run_dir: Path) -> BehavioralSignals:
    report = _read_text(run_dir / "report.md")
    raw: dict[str, Any] = {}

    raw["story_count"] = 0
    raw["stories_passed"] = 0
    raw["stories_failed"] = 0
    raw["iterations"] = 0

    if report:
        passed = re.findall(r"(?:passed|✅|PASS)", report, re.IGNORECASE)
        failed = re.findall(r"(?:failed|❌|FAIL)", report, re.IGNORECASE)
        raw["stories_passed"] = len(passed)
        raw["stories_failed"] = len(failed)
        raw["story_count"] = len(passed) + len(failed)
        source = "extracted"
    else:
        source = "missing"

    return BehavioralSignals(type="loop-until-done", raw=raw, source=source)


def _extract_flaky_test_diagnoser(run_dir: Path) -> BehavioralSignals:
    report = _read_text(run_dir / "report.md")
    if report is None:
        return BehavioralSignals(type="flaky-test-diagnoser", raw={}, source="missing")

    raw: dict[str, Any] = {}
    hyp_files = list(run_dir.glob("*hyp*.txt"))
    raw["hypothesis_count"] = len(hyp_files)
    raw["top_hypothesis"] = None
    raw["confidence"] = None
    raw["termination_label"] = None

    label_m = re.search(r"(?:label|termination)[=:]\s*(.+?)(?:\n|$)", report, re.IGNORECASE)
    if label_m:
        raw["termination_label"] = label_m.group(1).strip()

    return BehavioralSignals(type="flaky-test-diagnoser", raw=raw, source="extracted")


def _extract_autopilot(run_dir: Path) -> BehavioralSignals:
    raw: dict[str, Any] = {}
    raw["ambiguity_class"] = None
    raw["routed_to"] = None
    raw["verdict"] = None

    report = _read_text(run_dir / "report.md")
    if report:
        source = "extracted"
    else:
        source = "partial" if any(run_dir.glob("*.txt")) else "missing"

    return BehavioralSignals(type="autopilot", raw=raw, source=source)


_EXTRACTORS: dict[str, Callable[[Path], BehavioralSignals]] = {
    "deep-qa": _extract_deep_qa,
    "deep-design": _extract_deep_design,
    "deep-debug": _extract_deep_debug,
    "deep-research": _extract_deep_research,
    "deep-plan": _extract_deep_plan,
    "proposal-reviewer": _extract_proposal_reviewer,
    "team": _extract_team,
    "loop-until-done": _extract_loop_until_done,
    "flaky-test-diagnoser": _extract_flaky_test_diagnoser,
    "autopilot": _extract_autopilot,
}
