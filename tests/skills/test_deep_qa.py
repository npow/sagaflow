"""Workflow-level tests for DeepQaWorkflow using time-skipped Temporal env.

Tests swap `spawn_subagent` with a role-dispatching fake that returns
deterministic structured outputs per role. All tests are hermetic — no real
Anthropic or claude -p calls.

Coverage:
  - Basic round-trip (report + INBOX entry)
  - Two-pass severity judges (pass-1 blind → pass-2 authoritative)
  - Rationalization auditor: clean path
  - Rationalization auditor: compromised × 2 → "Audit compromised" label
  - Research-type fact verification
  - Coverage enforcement (missing required category → extra round)
"""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.deep_qa.activities import read_text_file
from skills.deep_qa.workflow import DeepQaInput, DeepQaWorkflow


# ---------------------------------------------------------------------------
# Shared worker helper
# ---------------------------------------------------------------------------

_SANDBOX_RESTRICTIONS = SandboxRestrictions.default.with_passthrough_modules(
    "httpx", "anthropic", "sagaflow"
)


async def _run_workflow(
    tmp_path,
    fake_spawn,
    inp: DeepQaInput,
) -> str:
    """Boot a time-skipping env, run the workflow, return the result string."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepQaWorkflow],
            activities=[write_artifact, emit_finding, fake_spawn, read_text_file],
            workflow_runner=SandboxedWorkflowRunner(restrictions=_SANDBOX_RESTRICTIONS),
        ):
            return await env.client.execute_workflow(
                DeepQaWorkflow.run,
                inp,
                id=inp.run_id,
                task_queue=TASK_QUEUE,
            )


# ---------------------------------------------------------------------------
# Test 1: basic round-trip (replaces the old test, now with 6-critic cap)
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_basic(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Does it handle empty input?"},
                {"id": "a2", "dimension": "clarity", "question": "Is the API name self-explanatory?"},
            ])
        }
    if role == "critic":
        return {
            "DEFECTS": json.dumps([
                {
                    "id": "d1",
                    "title": "Empty-input path is undefined",
                    "severity": "major",
                    "dimension": "correctness",
                    "scenario": "Called with no arguments.",
                    "root_cause": "No input guard.",
                }
            ])
        }
    if role in ("judge-pass-1", "judge-pass-2"):
        return {
            "VERDICTS": json.dumps([
                {
                    "defect_id": "d1",
                    "severity": "major",
                    "confidence": "high",
                    "calibration": "confirm",
                    "rationale": "Scenario is concrete and realistic.",
                }
            ])
        }
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "All defects carried."}
    if role == "synth":
        return {"REPORT": "# QA Report (fake)\n\n1 major defect.\n"}
    return {}


async def test_deep_qa_roundtrip_produces_report(tmp_path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("Hello world.\nThis is the artifact under QA.\n")

    result = await _run_workflow(
        tmp_path,
        _fake_basic,
        DeepQaInput(
            run_id="dq-1",
            artifact_path=str(artifact),
            artifact_type="doc",
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=1,
            notify=False,
        ),
    )

    assert "defects across" in result
    report_path = tmp_path / "run" / "qa-report.md"
    assert report_path.exists()
    assert "QA Report" in report_path.read_text()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dq-1" in inbox_text
    assert "DONE" in inbox_text


# ---------------------------------------------------------------------------
# Test 2: two-pass judge — pass-2 severity is authoritative
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_two_pass_judges(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Any null-deref risk?"},
            ])
        }
    if role == "critic":
        # Critic proposes "minor".
        return {
            "DEFECTS": json.dumps([
                {
                    "id": "judge-test-d1",
                    "title": "Null dereference in happy path",
                    "severity": "minor",   # critic proposes minor
                    "dimension": "correctness",
                    "scenario": "Input is None.",
                    "root_cause": "No guard.",
                }
            ])
        }
    if role == "judge-pass-1":
        # Pass-1 blind sees only the defect without severity — assigns "major".
        return {
            "VERDICTS": json.dumps([
                {
                    "defect_id": "judge-test-d1",
                    "severity": "major",
                    "confidence": "medium",
                    "calibration": "confirm",
                    "rationale": "Real null-deref in production path.",
                }
            ])
        }
    if role == "judge-pass-2":
        # Pass-2 informed upgrades to "critical".
        return {
            "VERDICTS": json.dumps([
                {
                    "defect_id": "judge-test-d1",
                    "severity": "critical",
                    "confidence": "high",
                    "calibration": "upgrade",
                    "rationale": "Null-deref crashes the service on first request.",
                }
            ])
        }
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "All defects carried."}
    if role == "synth":
        return {"REPORT": "# QA Report\n\n1 critical defect.\n"}
    return {}


async def test_two_pass_judge_severity_authoritative(tmp_path) -> None:
    """Pass-2 verdict (critical) must override critic-proposed (minor)."""
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("def foo(x): return x.bar\n")

    result = await _run_workflow(
        tmp_path,
        _fake_two_pass_judges,
        DeepQaInput(
            run_id="dq-judges",
            artifact_path=str(artifact),
            artifact_type="code",
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=1,
            notify=False,
        ),
    )

    # The result summary should reflect the pass-2 severity (critical), not critic (minor).
    assert "1 critical" in result
    assert "0 minor" in result or "minor" not in result.split("defects")[0]


# ---------------------------------------------------------------------------
# Test 3: rationalization auditor — clean path
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_auditor_clean(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {"ANGLES": json.dumps([
            {"id": "a1", "dimension": "correctness", "question": "Edge case coverage?"},
        ])}
    if role == "critic":
        return {"DEFECTS": json.dumps([
            {"id": "d-clean-1", "title": "Missing null check", "severity": "major",
             "dimension": "correctness", "scenario": "x=None", "root_cause": "No guard."}
        ])}
    if role in ("judge-pass-1", "judge-pass-2"):
        return {"VERDICTS": json.dumps([
            {"defect_id": "d-clean-1", "severity": "major",
             "confidence": "high", "calibration": "confirm", "rationale": "Valid."}
        ])}
    if role == "auditor":
        # First (and only) audit: clean.
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "Report matches verdicts."}
    if role == "synth":
        return {"REPORT": "# QA Report\n\nClean audit path.\n"}
    return {}


async def test_rationalization_auditor_clean(tmp_path) -> None:
    """Auditor returns clean → report assembled normally, no special label."""
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("def foo(): pass\n")

    result = await _run_workflow(
        tmp_path,
        _fake_auditor_clean,
        DeepQaInput(
            run_id="dq-audit-clean",
            artifact_path=str(artifact),
            artifact_type="code",
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=1,
            notify=False,
        ),
    )

    assert "Audit compromised" not in result
    report = (tmp_path / "run" / "qa-report.md").read_text()
    assert "QA Report" in report


# ---------------------------------------------------------------------------
# Test 4: rationalization auditor — compromised × 2 → special label
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_auditor_compromised(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {"ANGLES": json.dumps([
            {"id": "a1", "dimension": "security", "question": "Any injection risk?"},
        ])}
    if role == "critic":
        return {"DEFECTS": json.dumps([
            {"id": "d-comp-1", "title": "SQL injection", "severity": "critical",
             "dimension": "security", "scenario": "Unsanitized user input in query.",
             "root_cause": "No parameterization."}
        ])}
    if role in ("judge-pass-1", "judge-pass-2"):
        return {"VERDICTS": json.dumps([
            {"defect_id": "d-comp-1", "severity": "critical",
             "confidence": "high", "calibration": "confirm", "rationale": "Real SQLi."}
        ])}
    if role == "auditor":
        # Both audit attempts return compromised.
        return {
            "REPORT_FIDELITY": "compromised",
            "RATIONALE": "Defect severity was softened in draft.",
        }
    if role == "synth":
        return {"REPORT": "# QA Report\n\n(synth)\n"}
    return {}


async def test_rationalization_auditor_double_compromised(tmp_path) -> None:
    """Two consecutive compromised verdicts → 'Audit compromised' termination label."""
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("SELECT * FROM users WHERE id = '" + "' + user_input + '")

    result = await _run_workflow(
        tmp_path,
        _fake_auditor_compromised,
        DeepQaInput(
            run_id="dq-audit-compromised",
            artifact_path=str(artifact),
            artifact_type="code",
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=1,
            notify=False,
        ),
    )

    assert "Audit compromised" in result
    report_text = (tmp_path / "run" / "qa-report.md").read_text()
    # Report should carry the compromised-path caveat.
    assert "Coordinator drift" in report_text or "judge verdicts" in report_text.lower()


# ---------------------------------------------------------------------------
# Test 5: research-type fact verification
# ---------------------------------------------------------------------------

_verifier_called: list[str] = []


@activity.defn(name="spawn_subagent")
async def _fake_research_verify(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {"ANGLES": json.dumps([
            {"id": "a1", "dimension": "accuracy", "question": "Are citations accessible?"},
        ])}
    if role == "critic":
        return {"DEFECTS": json.dumps([])}
    if role in ("judge-pass-1", "judge-pass-2"):
        return {"VERDICTS": json.dumps([])}
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "No defects."}
    if role == "verifier":
        _verifier_called.append("called")
        return {
            "VERIFICATION": json.dumps({
                "checked_count": 5,
                "total_claims": 8,
                "accessible_rate": 0.8,
                "mismatches": 1,
            })
        }
    if role == "synth":
        return {"REPORT": "# QA Report\n\nVerification section present.\n"}
    return {}


async def test_research_type_runs_verifier(tmp_path) -> None:
    """artifact_type='research' must trigger the verifier activity."""
    _verifier_called.clear()
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("# Research Report\n\nClaim 1: X is true [source1].\n")

    await _run_workflow(
        tmp_path,
        _fake_research_verify,
        DeepQaInput(
            run_id="dq-research",
            artifact_path=str(artifact),
            artifact_type="research",
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=1,
            notify=False,
        ),
    )

    # Verifier must have been invoked.
    assert _verifier_called, "Verifier activity was not called for research artifact"
    # Report should exist and be written.
    assert (tmp_path / "run" / "qa-report.md").exists()


# ---------------------------------------------------------------------------
# Test 6: coverage enforcement — missing required category → extra critic round
# ---------------------------------------------------------------------------

_critic_call_count: list[int] = []
_coverage_angles_seen: list[str] = []


@activity.defn(name="spawn_subagent")
async def _fake_coverage_enforce(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        # Only covers "correctness"; leaves "error_handling", "security", "testability" uncovered
        # for a "code" artifact.
        return {"ANGLES": json.dumps([
            {"id": "a1", "dimension": "correctness", "question": "Logic correctness?"},
        ])}
    if role == "critic":
        _critic_call_count.append(1)
        # Track which dimensions are being critiqued (from user_prompt_path).
        return {"DEFECTS": json.dumps([])}
    if role in ("judge-pass-1", "judge-pass-2"):
        return {"VERDICTS": json.dumps([])}
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "No defects."}
    if role == "synth":
        return {"REPORT": "# QA Report\n\nCoverage enforced.\n"}
    return {}


async def test_coverage_enforcement_adds_critical_angles(tmp_path) -> None:
    """When required categories are uncovered, extra CRITICAL angles should be added."""
    _critic_call_count.clear()
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("def add(a, b): return a + b\n")

    result = await _run_workflow(
        tmp_path,
        _fake_coverage_enforce,
        DeepQaInput(
            run_id="dq-coverage",
            artifact_path=str(artifact),
            artifact_type="code",  # requires: correctness, error_handling, security, testability
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=3,  # give enough rounds for coverage enforcement to fire
            notify=False,
        ),
    )

    # Multiple critic calls expected: initial round + coverage enforcement rounds.
    # At minimum the first round (1 critic) + at least 1 coverage angle per missing category.
    assert len(_critic_call_count) > 1, (
        f"Expected >1 critic calls (coverage enforcement), got {len(_critic_call_count)}"
    )
    assert (tmp_path / "run" / "qa-report.md").exists()
    assert "defects across" in result


# ---------------------------------------------------------------------------
# Test 7: qa-report.md gets real synth content — not a 1-line stub
# ---------------------------------------------------------------------------


async def test_report_has_real_content_from_synth(tmp_path) -> None:
    """qa-report.md must contain the full synth output, not just a header."""
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("Hello world.\nThis is the artifact under QA.\n")

    result = await _run_workflow(
        tmp_path,
        _fake_basic,
        DeepQaInput(
            run_id="dq-real-content",
            artifact_path=str(artifact),
            artifact_type="doc",
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=1,
            notify=False,
        ),
    )

    report = (tmp_path / "run" / "qa-report.md").read_text()
    # Must have more than just "# QA Report" — the synth fake returns a body.
    assert len(report.strip()) > len("# QA Report"), (
        f"qa-report.md is a stub ({len(report)} bytes): {report!r}"
    )
    assert "1 major defect" in report


# ---------------------------------------------------------------------------
# Test 8: malformed synth falls back to audited draft report
# ---------------------------------------------------------------------------


@activity.defn(name="spawn_subagent")
async def _fake_malformed_synth(inp: SpawnSubagentInput) -> dict[str, str]:
    role = inp.role
    if role == "dim-discover":
        return {
            "ANGLES": json.dumps([
                {"id": "a1", "dimension": "correctness", "question": "Does it handle empty input?"},
            ])
        }
    if role == "critic":
        return {
            "DEFECTS": json.dumps([
                {
                    "id": "d1",
                    "title": "Empty-input crash",
                    "severity": "major",
                    "dimension": "correctness",
                    "scenario": "Called with no arguments.",
                    "root_cause": "No input guard.",
                }
            ])
        }
    if role in ("judge-pass-1", "judge-pass-2"):
        return {
            "VERDICTS": json.dumps([
                {
                    "defect_id": "d1",
                    "severity": "major",
                    "confidence": "high",
                    "calibration": "confirm",
                    "rationale": "Scenario is concrete.",
                }
            ])
        }
    if role == "auditor":
        return {"REPORT_FIDELITY": "clean", "RATIONALE": "All defects carried."}
    if role == "synth":
        # Malformed: return sentinel with no REPORT key.
        return {"_sagaflow_malformed": "1", "_error": "no block", "_raw": ""}
    return {}


async def test_malformed_synth_falls_back_to_draft(tmp_path) -> None:
    """When synth returns malformed output, qa-report.md gets the audited draft."""
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("def foo(): pass\n")

    result = await _run_workflow(
        tmp_path,
        _fake_malformed_synth,
        DeepQaInput(
            run_id="dq-malformed-synth",
            artifact_path=str(artifact),
            artifact_type="code",
            inbox_path=str(tmp_path / "INBOX.md"),
            run_dir=str(tmp_path / "run"),
            max_rounds=1,
            notify=False,
        ),
    )

    report = (tmp_path / "run" / "qa-report.md").read_text()
    # Must have real content from the draft — not empty, not just a header.
    assert len(report.strip()) > len("# QA Report"), (
        f"qa-report.md is a stub ({len(report)} bytes): {report!r}"
    )
    # Draft report includes structured defect info.
    assert "Empty-input crash" in report
    assert "correctness" in report.lower()
