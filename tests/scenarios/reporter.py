"""Scenario test reporter — JSON results and baseline comparison."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ScenarioResult:
    """Result of a single scenario test execution."""
    name: str
    skill: str
    passed: bool
    duration_seconds: float
    failure_modes: list[str] = field(default_factory=list)
    error: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class ScenarioReport:
    """Aggregate report of a scenario test run."""
    timestamp: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    results: list[ScenarioResult] = field(default_factory=list)

    def record(self, result: ScenarioResult) -> None:
        self.results.append(result)
        self.total += 1
        if result.passed:
            self.passed += 1
        else:
            self.failed += 1

    def summary(self) -> dict:
        return {
            "timestamp": self.timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": (
                f"{self.passed / self.total * 100:.1f}%" if self.total else "N/A"
            ),
            "by_skill": self._by_skill(),
            "by_failure_mode": self._by_failure_mode(),
        }

    def _by_skill(self) -> dict[str, dict]:
        skills: dict[str, dict] = {}
        for r in self.results:
            if r.skill not in skills:
                skills[r.skill] = {"total": 0, "passed": 0, "failed": 0}
            skills[r.skill]["total"] += 1
            if r.passed:
                skills[r.skill]["passed"] += 1
            else:
                skills[r.skill]["failed"] += 1
        return skills

    def _by_failure_mode(self) -> dict[str, int]:
        modes: dict[str, int] = {}
        for r in self.results:
            for m in r.failure_modes:
                modes[m] = modes.get(m, 0) + 1
        return modes

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.summary()
        data["results"] = [asdict(r) for r in self.results]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load_json(cls, path: Path) -> ScenarioReport:
        data = json.loads(path.read_text(encoding="utf-8"))
        report = cls(
            timestamp=data.get("timestamp", ""),
            total=data.get("total", 0),
            passed=data.get("passed", 0),
            failed=data.get("failed", 0),
        )
        for r in data.get("results", []):
            report.results.append(
                ScenarioResult(
                    **{
                        k: v
                        for k, v in r.items()
                        if k in ScenarioResult.__dataclass_fields__
                    }
                )
            )
        return report


def compare_reports(baseline: Path, current: Path) -> dict:
    """Compare two scenario reports and return a diff summary."""
    base = ScenarioReport.load_json(baseline)
    curr = ScenarioReport.load_json(current)

    base_by_name = {r.name: r for r in base.results}
    curr_by_name = {r.name: r for r in curr.results}

    regressions = [n for n, cr in curr_by_name.items()
                   if n in base_by_name and base_by_name[n].passed and not cr.passed]
    improvements = [n for n, cr in curr_by_name.items()
                    if n in base_by_name and not base_by_name[n].passed and cr.passed]
    new_scenarios = [n for n in curr_by_name if n not in base_by_name]
    removed = [n for n in base_by_name if n not in curr_by_name]

    return {
        "baseline_total": base.total,
        "current_total": curr.total,
        "regressions": regressions,
        "improvements": improvements,
        "new_scenarios": new_scenarios,
        "removed": removed,
        "has_regressions": len(regressions) > 0,
    }
