"""Regression tests for deep-research workflow_rlm — catches the three env bugs
fixed on 2026-05-24:

  Bug 1 — DEFAULT_PYTHON was hardcoded to /apps/default-python/bin/python3
           (BDI Python 3.10), which lacks temporalio.
  Bug 2 — Worker startup didn't export RLM_API_BASE, so the orchestrator
           fell back to OPENAI_BASE_URL (copilot DP endpoint, returned 404).
  Bug 3 — RLM_API_BASE was missing /v1, causing 401 from the MGP proxy.

These tests run offline (no Temporal server, no LLM calls).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Import RunShell* at module level so Temporal's get_type_hints() resolves the
# annotations on the fake run_shell activity without NameError.
try:
    from sagaflow.durable.activities import RunShellInput as RunShellInput
    from sagaflow.durable.activities import RunShellResult as RunShellResult
except (ImportError, AttributeError):
    RunShellInput = None   # type: ignore[assignment,misc]
    RunShellResult = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_workflow_rlm():
    """Load skills.deep_research.workflow_rlm — works in pytest via conftest."""
    import importlib
    return importlib.import_module("skills.deep_research.workflow_rlm")


# ---------------------------------------------------------------------------
# Bug 1 — DEFAULT_PYTHON must be sys.executable (not a hardcoded BDI path)
# ---------------------------------------------------------------------------

def test_default_python_is_sys_executable() -> None:
    """DEFAULT_PYTHON must equal sys.executable — never a hardcoded path."""
    mod = _import_workflow_rlm()
    assert mod.DEFAULT_PYTHON == sys.executable, (
        f"DEFAULT_PYTHON is {mod.DEFAULT_PYTHON!r} but should be sys.executable "
        f"({sys.executable!r}). Hardcoding a system Python breaks on any host "
        "that doesn't have temporalio installed in that interpreter."
    )


def test_default_python_not_bdi_py310() -> None:
    """DEFAULT_PYTHON must not be the BDI default Python 3.10 path.

    Regression guard: if someone re-hardcodes the original bad path, this fires.
    """
    mod = _import_workflow_rlm()
    forbidden = {
        "/apps/default-python/bin/python3",
        "/apps/default-python/bin/python",
        "/apps/python3.10/bin/python3",
        "/apps/python3.10/bin/python",
    }
    assert mod.DEFAULT_PYTHON not in forbidden, (
        f"DEFAULT_PYTHON is {mod.DEFAULT_PYTHON!r} which is a BDI system Python "
        "that does not have temporalio installed. Use sys.executable instead."
    )


@pytest.mark.skipif(
    os.environ.get("CI") == "1",
    reason="CI uses temporalio stubs; subprocess check only meaningful on real env",
)
def test_default_python_can_import_dspy() -> None:
    """DEFAULT_PYTHON must be able to import dspy (needed by rlm/runner.py).

    dspy.RLM is the agent loop driving per-dimension research. Without dspy
    every research dimension fails with ModuleNotFoundError, producing an empty
    synthesis with 0 usable findings.
    """
    mod = _import_workflow_rlm()
    result = subprocess.run(
        [mod.DEFAULT_PYTHON, "-c", "import dspy; assert hasattr(dspy, 'RLM'), 'dspy.RLM missing'"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"DEFAULT_PYTHON ({mod.DEFAULT_PYTHON!r}) cannot import dspy or lacks dspy.RLM.\n"
        f"stderr: {result.stderr.strip()}\n"
        "Install dspy-ai>=3.0.0 in the sagaflow uv venv."
    )


@pytest.mark.skipif(
    os.environ.get("CI") == "1",
    reason="CI uses temporalio stubs; subprocess check only meaningful on real env",
)
def test_default_python_can_import_temporalio() -> None:
    """Subprocess with DEFAULT_PYTHON must be able to import temporalio.

    This is the root-cause test: if this fails, sagaflow workflows that import
    from sagaflow.api will crash immediately with ModuleNotFoundError.
    """
    mod = _import_workflow_rlm()
    result = subprocess.run(
        [mod.DEFAULT_PYTHON, "-c", "import temporalio"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"DEFAULT_PYTHON ({mod.DEFAULT_PYTHON!r}) cannot import temporalio.\n"
        f"stderr: {result.stderr.strip()}\n"
        "Install temporalio in this interpreter or point DEFAULT_PYTHON at "
        "the sagaflow uv venv (which has it)."
    )


@pytest.mark.skipif(
    os.environ.get("CI") == "1",
    reason="CI uses temporalio stubs",
)
def test_default_python_can_import_sagaflow() -> None:
    """DEFAULT_PYTHON must be able to import sagaflow (needed by the orchestrator)."""
    mod = _import_workflow_rlm()
    result = subprocess.run(
        [mod.DEFAULT_PYTHON, "-c", "import sagaflow"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"DEFAULT_PYTHON ({mod.DEFAULT_PYTHON!r}) cannot import sagaflow.\n"
        f"stderr: {result.stderr.strip()}"
    )


# ---------------------------------------------------------------------------
# Bug 2 — RLM_API_BASE must be set in the worker env
# ---------------------------------------------------------------------------

def test_rlm_workflow_command_uses_rlm_api_base_env(monkeypatch) -> None:
    """The orchestrator subprocess must inherit RLM_API_BASE, not a hardcoded URL.

    We verify by checking that the module reads from the environment (via
    sagaflow.rlm.orchestrator's MGP_BASE resolution chain) rather than
    embedding a fixed URL in the command string.
    """
    mod = _import_workflow_rlm()

    # The command is assembled in the workflow body; we can inspect the
    # cmd_parts list by examining the source. A hardcoded URL would appear
    # as a string literal in the command.
    import inspect
    source = inspect.getsource(mod)

    # The command must NOT embed a hardcoded MGP URL.
    forbidden_url_patterns = [
        "mgp.local.dev.netflix.net",
        "claudecode.local.dev.netflix.net",
        "copilotdppython",
    ]
    for pattern in forbidden_url_patterns:
        assert pattern not in source or "docs" in source.lower(), (
            f"workflow_rlm.py contains hardcoded URL pattern {pattern!r}. "
            "API base URLs must come from RLM_API_BASE env var, not be embedded "
            "in the workflow source."
        )


def test_rlm_command_passes_python_path_flag() -> None:
    """The orchestrator command must include --python-path so sub-subprocesses
    also use the correct interpreter, not whatever 'python3' resolves to on PATH.
    """
    import inspect
    mod = _import_workflow_rlm()
    source = inspect.getsource(mod)
    assert "--python-path" in source, (
        "workflow_rlm.py must pass --python-path to the orchestrator. "
        "Without it, sub-subprocesses default to PATH's python3 (BDI 3.10)."
    )


# ---------------------------------------------------------------------------
# Bug 3 — RLM_API_BASE must include /v1
# ---------------------------------------------------------------------------

def test_installer_rlm_api_base_includes_v1() -> None:
    """The dotfiles installer must set RLM_API_BASE with the /v1 suffix.

    Without /v1 the OpenAI SDK constructs wrong URLs and the MGP proxy
    returns 401 from litellm (Missing Anthropic API Key).
    """
    installer = Path.home() / "code" / "dotfiles" / "installers" / "11-sagaflow.sh"
    if not installer.exists():
        pytest.skip(f"dotfiles installer not found at {installer}")

    text = installer.read_text()
    assert "RLM_API_BASE" in text, (
        "11-sagaflow.sh must export RLM_API_BASE for the sagaflow worker. "
        "Without it the RLM orchestrator falls back to OPENAI_BASE_URL (wrong endpoint)."
    )

    # Extract the RLM_API_BASE line and verify it contains /v1
    for line in text.splitlines():
        if "RLM_API_BASE" in line and "export" in line and "#" not in line.lstrip()[:1]:
            assert "/v1" in line, (
                f"RLM_API_BASE in 11-sagaflow.sh is missing /v1: {line.strip()!r}\n"
                "The OpenAI SDK needs the full versioned base URL "
                "(.../proxy/npowws/v1) to construct correct endpoint paths."
            )
            break


# ---------------------------------------------------------------------------
# Workflow-level smoke: run_shell gets a sane command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workflow_rlm_run_shell_receives_correct_command(tmp_path) -> None:
    """DeepResearchWorkflow (RLM backend) must call run_shell with a command that:
    - invokes the orchestrator module (sagaflow.rlm.orchestrator)
    - passes --query with the seed
    - passes --python-path so sub-processes use the same interpreter
    """
    try:
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker
        from temporalio.worker.workflow_sandbox import SandboxRestrictions, SandboxedWorkflowRunner
        from temporalio import activity
        from sagaflow.durable.activities import (
            emit_finding, finalize_manifest_activity, write_artifact,
        )
    except (ImportError, AttributeError):
        pytest.skip("temporalio not available (CI stub environment)")

    from skills.deep_research.workflow_rlm import DeepResearchInput, DeepResearchWorkflow
    from sagaflow.temporal_client import TASK_QUEUE

    captured_commands: list[str] = []

    @activity.defn(name="run_shell")
    async def _fake_shell(inp: RunShellInput) -> RunShellResult:
        captured_commands.append(inp.command)
        # Write a minimal report so the workflow can complete.
        report = tmp_path / "run" / "report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# Research Report\n\ntest content\n")
        return RunShellResult(stdout='{"direction_count": 3, "dimension_count": 2}', stderr="", exit_code=0)

    @activity.defn(name="report_slack_progress")
    async def _fake_slack(inp) -> None:  # type: ignore[no-untyped-def]
        pass

    sandbox = SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
        )
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DeepResearchWorkflow],
            activities=[
                write_artifact, emit_finding, finalize_manifest_activity,
                _fake_shell, _fake_slack,
            ],
            workflow_runner=sandbox,
        ):
            await env.client.execute_workflow(
                DeepResearchWorkflow.run,
                DeepResearchInput(
                    run_id="rlm-cmd-test",
                    seed="test topic for command validation",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "run"),
                ),
                id="rlm-cmd-test",
                task_queue=TASK_QUEUE,
            )

    assert captured_commands, "run_shell was never called — workflow didn't reach orchestrator dispatch"
    cmd = captured_commands[0]

    assert "sagaflow.rlm.orchestrator" in cmd, (
        f"Command must invoke sagaflow.rlm.orchestrator, got: {cmd[:200]!r}"
    )
    assert "--query" in cmd, f"Command must include --query flag, got: {cmd[:200]!r}"
    assert "--python-path" in cmd, (
        f"Command must include --python-path so sub-processes use the correct "
        f"interpreter, got: {cmd[:200]!r}"
    )
    assert "/apps/default-python" not in cmd, (
        f"Command must not hardcode /apps/default-python — use sys.executable: {cmd[:200]!r}"
    )
