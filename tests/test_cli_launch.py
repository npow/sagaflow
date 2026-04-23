from unittest.mock import patch

from click.testing import CliRunner

from sagaflow.cli import main


def test_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for sub in ["launch", "list", "show", "inbox", "worker", "hook", "doctor"]:
        assert sub in result.output


def test_launch_prints_workflow_id() -> None:
    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all"),
        patch("sagaflow.cli._ensure_hook_installed"),
        patch("sagaflow.cli._ensure_worker_running"),
        patch("sagaflow.cli._start_workflow", return_value="hello-world-test-1") as start,
        patch("sagaflow.cli._await_workflow", return_value="greeting"),
    ):
        result = runner.invoke(main, ["launch", "hello-world", "--name", "alice"])
    assert result.exit_code == 0
    assert "hello-world-test-1" in result.output
    start.assert_called_once()


def test_launch_without_await_does_not_block() -> None:
    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all"),
        patch("sagaflow.cli._ensure_hook_installed"),
        patch("sagaflow.cli._ensure_worker_running"),
        patch("sagaflow.cli._start_workflow", return_value="wf-id") as start,
        patch("sagaflow.cli._await_workflow") as await_,
    ):
        result = runner.invoke(main, ["launch", "hello-world", "--name", "bob"])
    assert result.exit_code == 0
    start.assert_called_once()
    await_.assert_not_called()


def test_launch_await_blocks_on_result() -> None:
    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all"),
        patch("sagaflow.cli._ensure_hook_installed"),
        patch("sagaflow.cli._ensure_worker_running"),
        patch("sagaflow.cli._start_workflow", return_value="wf-id"),
        patch("sagaflow.cli._await_workflow", return_value="hello, bob") as await_,
    ):
        result = runner.invoke(main, ["launch", "hello-world", "--name", "bob", "--await"])
    assert result.exit_code == 0
    await_.assert_called_once()
    assert "hello, bob" in result.output


def test_launch_missing_required_arg_surfaces_before_worker() -> None:
    # deep-qa requires --path; launch must fail with a UsageError before touching
    # preflight / hook install / worker spawn. Prior to this test, missing-arg
    # errors were hidden behind an opaque "worker did not become ready" failure.
    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all") as preflight,
        patch("sagaflow.cli._ensure_hook_installed") as hook_install,
        patch("sagaflow.cli._ensure_worker_running") as worker,
        patch("sagaflow.cli._start_workflow") as start,
    ):
        result = runner.invoke(main, ["launch", "deep-qa"])
    assert result.exit_code != 0
    combined = (result.output + str(result.exception or "")).lower()
    assert "path" in combined or "proposal" in combined or "artifact" in combined
    preflight.assert_not_called()
    hook_install.assert_not_called()
    worker.assert_not_called()
    start.assert_not_called()


def test_launch_unknown_skill_surfaces_before_worker() -> None:
    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all") as preflight,
        patch("sagaflow.cli._ensure_hook_installed") as hook_install,
        patch("sagaflow.cli._ensure_worker_running") as worker,
        patch("sagaflow.cli._start_workflow") as start,
    ):
        result = runner.invoke(main, ["launch", "nonexistent-skill"])
    assert result.exit_code != 0
    assert "nonexistent-skill" in result.output or "unknown" in result.output.lower()
    preflight.assert_not_called()
    hook_install.assert_not_called()
    worker.assert_not_called()
    start.assert_not_called()
