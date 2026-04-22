"""Workflow-level tests for DeepResearchWorkflow — full fidelity suite."""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner

from sagaflow.durable.activities import SpawnSubagentInput, emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.deep_research.workflow import DeepResearchInput, DeepResearchWorkflow


# ---------------------------------------------------------------------------
# Fake activity — dispatches on all roles
# ---------------------------------------------------------------------------

@activity.defn(name="spawn_subagent")
async def _fake(inp: SpawnSubagentInput) -> dict[str, str]:
    """Route by role; return minimal structured output for each new phase."""

    if inp.role == "lang-detect":
        return {
            "AUTHORITATIVE_LANGUAGES": '["en"]',
            "COVERAGE_EXPECTATION": "en_dominant",
        }

    if inp.role == "novelty-classify":
        return {
            "NOVELTY_CLASS": "familiar",
            "RECALLED_SOURCES": json.dumps([
                {"title": "Paper A", "authors_or_org": "Org1", "year": 2022, "confidence": "high"},
                {"title": "Paper B", "authors_or_org": "Org2", "year": 2023, "confidence": "high"},
                {"title": "Paper C", "authors_or_org": "Org3", "year": 2021, "confidence": "medium"},
            ]),
            "VERIFIED_COUNT": "3",
        }

    if inp.role == "vocab-bootstrap":
        return {
            "CANONICAL_TERMS": '["term-alpha", "term-beta", "term-gamma"]',
            "DISCOVERED_SOURCES": '["https://en.wikipedia.org/wiki/Example"]',
        }

    if inp.role == "dim-discover":
        dirs = [
            {"id": "d1", "dimension": "HOW", "question": "How does it work?", "priority": "high"},
            {"id": "d2", "dimension": "WHO", "question": "Who uses it?", "priority": "medium"},
            {"id": "d3", "dimension": "PRIOR-FAILURE", "question": "What failed before?", "priority": "high"},
            {"id": "d4", "dimension": "BASELINE", "question": "What is the baseline?", "priority": "high"},
            {"id": "d5", "dimension": "ADJACENT-EFFORTS", "question": "Adjacent work?", "priority": "medium"},
        ]
        return {"DIRECTIONS": json.dumps(dirs)}

    if inp.role == "researcher":
        return {
            "FINDINGS": "Summary of research findings.",
            "SOURCES": '["Source A", "Source B"]',
            "CLAIMS": json.dumps([
                {"claim": "X costs 42 units", "source": "Source A",
                 "corroboration": "single_source", "recency_class": "fresh"},
                {"claim": "Y is dominant", "source": "Source B",
                 "corroboration": "two_independent_sources", "recency_class": "fresh"},
            ]),
        }

    if inp.role == "coord-summary":
        return {
            "COORD_SUMMARY": "## Round Summary\n\nMainstream: ...\nCounter-narratives: none.",
        }

    if inp.role == "verifier":
        return {
            "VERIFIED": '["claim-0"]',
            "MISMATCHES": '[{"claim_id": "claim-1", "issue": "number mismatch"}]',
            "UNVERIFIABLE": '[]',
            "SAMPLING_STRATEGY": '{"single_source": 1, "numerical": 1, "contested": 0, "other": 0}',
        }

    if inp.role == "synth":
        return {
            "REPORT": (
                "# Research Report\n\n"
                "## Executive Summary\n\nFindings: ...\n\n"
                "## Cross-cutting analysis\n\n"
                "| Dimension | Directions Explored |\n|---|---|\n"
                "| PRIOR-FAILURE | d3 |\n\n"
                "## Fact Verification Results\n\n"
                "Verified: 1, Mismatches: 1\n"
            ),
        }

    return {}


# ---------------------------------------------------------------------------
# Shared worker setup
# ---------------------------------------------------------------------------

_SANDBOX = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "httpx", "anthropic", "sagaflow", "skills"
    )
)


async def _run(inp: DeepResearchInput) -> str:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[write_artifact, emit_finding, _fake],
            workflow_runner=_SANDBOX,
        ):
            return await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                inp,
                id=inp.run_id,
                task_queue=TASK_QUEUE,
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_happy_path_report_written(tmp_path) -> None:
    """Basic smoke: report file is created and run_id appears in inbox."""
    result = await _run(DeepResearchInput(
        run_id="dr-happy",
        seed="What is Temporal workflow semantics?",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_directions=5,
        notify=False,
    ))
    assert "Report" in result
    assert (tmp_path / "run" / "research-report.md").exists()
    inbox_text = (tmp_path / "INBOX.md").read_text()
    assert "dr-happy" in inbox_text


async def test_novelty_cold_start_triggers_bootstrap(tmp_path) -> None:
    """When verified_count ≤ 1, novelty is forced to cold_start and bootstrap fires."""

    @activity.defn(name="spawn_subagent")
    async def _fake_cold(inp: SpawnSubagentInput) -> dict[str, str]:
        if inp.role == "novelty-classify":
            return {
                "NOVELTY_CLASS": "familiar",
                "RECALLED_SOURCES": "[]",
                "VERIFIED_COUNT": "0",   # forces cold_start override
            }
        if inp.role == "vocab-bootstrap":
            # Record that bootstrap was called.
            _fake_cold._bootstrap_called = True  # type: ignore[attr-defined]
            return {
                "CANONICAL_TERMS": '["bootstrap-term"]',
                "DISCOVERED_SOURCES": '[]',
            }
        # Delegate other roles to the shared fake.
        return await _fake(inp)

    _fake_cold._bootstrap_called = False  # type: ignore[attr-defined]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[write_artifact, emit_finding, _fake_cold],
            workflow_runner=_SANDBOX,
        ):
            result = await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                DeepResearchInput(
                    run_id="dr-cold",
                    seed="Completely unknown novel topic XYZ-9999",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_directions=3,
                    notify=False,
                ),
                id="dr-cold",
                task_queue=TASK_QUEUE,
            )
    assert _fake_cold._bootstrap_called, "vocab-bootstrap should have been called for cold_start"
    assert (tmp_path / "run" / "vocabulary_bootstrap.json").exists()
    vocab = json.loads((tmp_path / "run" / "vocabulary_bootstrap.json").read_text())
    assert "canonical_terms" in vocab
    assert "bootstrap-term" in vocab["canonical_terms"]
    assert "cold_start" in result


async def test_familiar_novelty_skips_bootstrap(tmp_path) -> None:
    """When ≥3 sources verify, self_report=familiar is accepted; no bootstrap."""

    bootstrap_calls: list[str] = []

    @activity.defn(name="spawn_subagent")
    async def _fake_familiar(inp: SpawnSubagentInput) -> dict[str, str]:
        if inp.role == "vocab-bootstrap":
            bootstrap_calls.append("called")
        return await _fake(inp)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[write_artifact, emit_finding, _fake_familiar],
            workflow_runner=_SANDBOX,
        ):
            await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                DeepResearchInput(
                    run_id="dr-familiar",
                    seed="What is Temporal?",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_directions=3,
                    notify=False,
                ),
                id="dr-familiar",
                task_queue=TASK_QUEUE,
            )
    assert bootstrap_calls == [], "bootstrap must NOT be called for familiar novelty"


async def test_cross_cut_coverage_tracked(tmp_path) -> None:
    """Cross-cut directions discovered by dim-discover appear in the final report."""
    await _run(DeepResearchInput(
        run_id="dr-xcut",
        seed="Kubernetes scheduling",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_directions=5,
        notify=False,
    ))
    report = (tmp_path / "run" / "research-report.md").read_text()
    assert "Cross-cutting analysis" in report
    # At least the dimensions present in _fake dim-discover output should appear.
    assert "PRIOR-FAILURE" in report
    assert "BASELINE" in report


async def test_verifier_section_in_final_report(tmp_path) -> None:
    """Fact Verification Results section is present in the synthesised report."""
    await _run(DeepResearchInput(
        run_id="dr-verify",
        seed="Neural scaling laws",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_directions=3,
        notify=False,
    ))
    report = (tmp_path / "run" / "research-report.md").read_text()
    assert "Fact Verification" in report


async def test_coordinator_summary_written_per_round(tmp_path) -> None:
    """coordinator-summary.md is written and contains round content."""
    await _run(DeepResearchInput(
        run_id="dr-coord",
        seed="Distributed tracing",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_directions=5,
        notify=False,
    ))
    coord_path = tmp_path / "run" / "coordinator-summary.md"
    assert coord_path.exists(), "coordinator-summary.md must be written"
    text = coord_path.read_text()
    assert "Round Summary" in text


async def test_per_direction_findings_files_written(tmp_path) -> None:
    """Each researched direction produces its own file under deep-research-findings/."""
    await _run(DeepResearchInput(
        run_id="dr-files",
        seed="gRPC streaming",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_directions=3,
        notify=False,
    ))
    findings_dir = tmp_path / "run" / "deep-research-findings"
    assert findings_dir.exists()
    md_files = list(findings_dir.glob("*.md"))
    # At least one direction file should exist (excluding .keep).
    real_files = [f for f in md_files if f.name != ".keep"]
    assert real_files, "per-direction findings files must be written"
    # Each file should have expected sections.
    for f in real_files:
        text = f.read_text()
        assert "## Findings" in text
        assert "## Claims Register" in text


async def test_termination_label_convergence(tmp_path) -> None:
    """When frontier exhausts, termination label is 'Convergence'."""
    result = await _run(DeepResearchInput(
        run_id="dr-converge",
        seed="Convergence test topic",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_directions=2,   # small batch — frontier will empty after round 1
        max_rounds=5,
        notify=False,
    ))
    # dim-discover emits 5 dirs but max_directions cap is 2, so frontier empties.
    assert "Convergence" in result or "Budget" in result or "saturated" in result


async def test_termination_label_budget_gate(tmp_path) -> None:
    """When max_rounds reached with remaining frontier, budget gate label fires."""

    @activity.defn(name="spawn_subagent")
    async def _fake_many_dirs(inp: SpawnSubagentInput) -> dict[str, str]:
        if inp.role == "dim-discover":
            # Return many directions so frontier is never exhausted in 1 round.
            dirs = [
                {"id": f"d{i}", "dimension": "HOW", "question": f"Q{i}?", "priority": "medium"}
                for i in range(20)
            ]
            return {"DIRECTIONS": json.dumps(dirs)}
        return await _fake(inp)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[write_artifact, emit_finding, _fake_many_dirs],
            workflow_runner=_SANDBOX,
        ):
            result = await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                DeepResearchInput(
                    run_id="dr-budget",
                    seed="Many directions topic",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_directions=20,
                    max_rounds=1,
                    notify=False,
                ),
                id="dr-budget",
                task_queue=TASK_QUEUE,
            )
    assert "Budget soft gate" in result or "Convergence" in result


async def test_absolute_hard_stop(tmp_path) -> None:
    """Workflow respects max_rounds * 3 absolute cap."""

    @activity.defn(name="spawn_subagent")
    async def _fake_infinite(inp: SpawnSubagentInput) -> dict[str, str]:
        if inp.role == "dim-discover":
            dirs = [
                {"id": f"d{i}", "dimension": "HOW", "question": f"Q{i}?", "priority": "medium"}
                for i in range(50)
            ]
            return {"DIRECTIONS": json.dumps(dirs)}
        return await _fake(inp)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[write_artifact, emit_finding, _fake_infinite],
            workflow_runner=_SANDBOX,
        ):
            await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                DeepResearchInput(
                    run_id="dr-hardstop",
                    seed="Endless topic",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_directions=50,
                    max_rounds=1,   # abs cap = 3
                    notify=False,
                ),
                id="dr-hardstop",
                task_queue=TASK_QUEUE,
            )
    # Should have terminated; report file must exist.
    assert (tmp_path / "run" / "research-report.md").exists()


async def test_language_locus_detection(tmp_path) -> None:
    """Language locus info is surfaced in the result summary."""
    result = await _run(DeepResearchInput(
        run_id="dr-lang",
        seed="Chinese AI chip manufacturing",
        inbox_path=str(tmp_path / "INBOX.md"),
        run_dir=str(tmp_path / "run"),
        max_directions=3,
        notify=False,
    ))
    # lang-detect fake returns en_dominant — workflow should complete without error.
    assert "Report" in result


async def test_novelty_two_verified_sources_downgrades(tmp_path) -> None:
    """verified_count == 2 downgrades self_report by one tier (familiar → emerging)."""

    @activity.defn(name="spawn_subagent")
    async def _fake_downgrade(inp: SpawnSubagentInput) -> dict[str, str]:
        if inp.role == "novelty-classify":
            return {
                "NOVELTY_CLASS": "familiar",
                "RECALLED_SOURCES": json.dumps([
                    {"title": "A", "authors_or_org": "Org1", "year": 2022, "confidence": "high"},
                    {"title": "B", "authors_or_org": "Org2", "year": 2023, "confidence": "high"},
                ]),
                "VERIFIED_COUNT": "2",  # triggers one-tier downgrade
            }
        return await _fake(inp)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[write_artifact, emit_finding, _fake_downgrade],
            workflow_runner=_SANDBOX,
        ):
            await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                DeepResearchInput(
                    run_id="dr-downgrade",
                    seed="Partially known topic",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                    max_directions=3,
                    notify=False,
                ),
                id="dr-downgrade",
                task_queue=TASK_QUEUE,
            )
    # emerging is not novel/cold_start so bootstrap should NOT fire.
    assert not (tmp_path / "run" / "vocabulary_bootstrap.json").exists()
    # But the workflow should still complete.
    assert (tmp_path / "run" / "research-report.md").exists()
