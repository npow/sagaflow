"""Scenario test conftest — JSON report generation plugin.

When ``--scenario-report=PATH`` is passed to pytest, this plugin collects
per-scenario pass/fail/duration and writes a JSON report at session end.
The report can then be compared via ``sagaflow test compare``.
"""

from __future__ import annotations

from pathlib import Path


def pytest_addoption(parser):
    parser.addoption(
        "--scenario-report",
        default=None,
        help="Write scenario JSON report to this path.",
    )


def pytest_configure(config):
    config._scenario_results = {}


def pytest_runtest_makereport(item, call):
    if call.when != "call":
        return
    if "/tests/scenarios/" not in str(item.fspath).replace("\\", "/"):
        return

    config = item.config
    config._scenario_results[item.nodeid] = {
        "passed": call.excinfo is None,
        "duration_s": round(call.duration, 3),
        "error": str(call.excinfo.value) if call.excinfo else None,
    }


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--scenario-report", default=None)
    if not path:
        return

    from tests.scenarios.registry import SCENARIO_REGISTRY
    from tests.scenarios.reporter import ScenarioReport, ScenarioResult

    report = ScenarioReport()
    results = session.config._scenario_results

    for _key, meta in sorted(SCENARIO_REGISTRY.items()):
        result = None
        for nodeid, res in results.items():
            if nodeid.endswith(meta.name):
                result = res
                break

        report.record(ScenarioResult(
            name=meta.name,
            skill=meta.skill,
            passed=result["passed"] if result else False,
            duration_seconds=result["duration_s"] if result else 0.0,
            failure_modes=meta.failure_modes,
            error=result["error"] if result else None,
            tags=meta.tags,
        ))

    report.save_json(Path(path))
