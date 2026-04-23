"""CLI fallback dispatch tests for the generic interpreter.

The CLI treats ``sagaflow launch <skill>`` as:
  1. If ``<skill>`` is a registered sagaflow skill, dispatch to its workflow.
  2. Else if ``~/.claude/skills/<skill>/SKILL.md`` exists, dispatch to the generic
     interpreter with ``_target_skill=<skill>`` threaded through cli_args.
  3. Else fail cleanly with a UsageError naming the missing SKILL.md path.

These tests mock ``_start_workflow`` to capture the skill + args actually routed
without starting Temporal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from sagaflow.cli import main


def _make_claude_skill(root: Path, skill_name: str) -> Path:
    """Create a fake claude-skills dir layout with ``<skill>/SKILL.md``."""
    skill_dir = root / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"# {skill_name}\n\nFake SKILL.md body for unit test.\n",
        encoding="utf-8",
    )
    return skill_md


def test_launch_known_skill_uses_registered_workflow() -> None:
    """hello-world keeps dispatching to HelloWorldWorkflow, not generic."""
    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all"),
        patch("sagaflow.cli._ensure_hook_installed"),
        patch("sagaflow.cli._ensure_worker_running"),
        patch("sagaflow.cli._start_workflow", return_value="hello-world-test-1") as start,
    ):
        result = runner.invoke(main, ["launch", "hello-world", "--name", "alice"])
    assert result.exit_code == 0, result.output
    start.assert_called_once()
    called_skill, called_args = start.call_args.args
    assert called_skill == "hello-world"
    # The fallback must NOT fire for known skills; no _target_skill in args.
    assert "_target_skill" not in called_args
    assert called_args.get("name") == "alice"


def test_launch_unknown_but_claude_skill_exists_dispatches_to_generic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown skill with a SKILL.md in CLAUDE_SKILLS_DIR routes to generic."""
    _make_claude_skill(tmp_path, "my-skill")
    monkeypatch.setenv("CLAUDE_SKILLS_DIR", str(tmp_path))

    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all"),
        patch("sagaflow.cli._ensure_hook_installed"),
        patch("sagaflow.cli._ensure_worker_running"),
        patch("sagaflow.cli._start_workflow", return_value="generic-test-1") as start,
    ):
        result = runner.invoke(main, ["launch", "my-skill"])
    assert result.exit_code == 0, result.output
    start.assert_called_once()
    called_skill, called_args = start.call_args.args
    # The CLI passes the *user-requested* skill name to _start_workflow; the
    # fallback rewrite happens inside _start_workflow via _resolve_skill, which
    # sets _target_skill in args so the generic build_input can load SKILL.md.
    # Validation also applies the fallback, so args should already carry
    # _target_skill by the time _start_workflow is invoked.
    assert called_args.get("_target_skill") == "my-skill"


def test_launch_unknown_skill_and_no_claude_skill_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truly-unknown skill produces a UsageError naming the missing SKILL.md path."""
    # Empty CLAUDE_SKILLS_DIR so no fallback can fire.
    monkeypatch.setenv("CLAUDE_SKILLS_DIR", str(tmp_path))

    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all") as preflight,
        patch("sagaflow.cli._ensure_hook_installed") as hook_install,
        patch("sagaflow.cli._ensure_worker_running") as worker,
        patch("sagaflow.cli._start_workflow") as start,
    ):
        result = runner.invoke(main, ["launch", "truly-nonexistent"])
    assert result.exit_code != 0
    combined = result.output.lower()
    assert "truly-nonexistent" in combined
    assert "skill.md" in combined
    # All the side-effect paths stayed cold -- the validator short-circuits.
    preflight.assert_not_called()
    hook_install.assert_not_called()
    worker.assert_not_called()
    start.assert_not_called()


def test_launch_generic_direct_requires_target_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sagaflow launch generic` without a claude-skills target must UsageError.

    When the user invokes ``generic`` directly (not via fallback), no
    ``_target_skill`` is set in cli_args. ``_build_input`` raises ValueError,
    which the validator translates into a UsageError surfaced to the user.
    """
    # Point CLAUDE_SKILLS_DIR at an empty dir so we can't accidentally fall
    # through to a real SKILL.md if one is installed on the host.
    monkeypatch.setenv("CLAUDE_SKILLS_DIR", str(tmp_path))

    runner = CliRunner()
    with (
        patch("sagaflow.cli._preflight_all") as preflight,
        patch("sagaflow.cli._ensure_hook_installed") as hook_install,
        patch("sagaflow.cli._ensure_worker_running") as worker,
        patch("sagaflow.cli._start_workflow") as start,
    ):
        result = runner.invoke(main, ["launch", "generic"])
    assert result.exit_code != 0
    combined = (result.output + str(result.exception or "")).lower()
    assert "_target_skill" in combined or "target_skill" in combined
    preflight.assert_not_called()
    hook_install.assert_not_called()
    worker.assert_not_called()
    start.assert_not_called()
