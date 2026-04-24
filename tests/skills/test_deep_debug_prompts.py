"""deep-debug DRY refactor: prompts loaded from claude-skills repo at build_input time."""

from __future__ import annotations

import pytest

from skills.deep_debug import _build_input


def test_build_input_loads_premortem_prompts_from_claude_skills_dir(
    tmp_path, monkeypatch
) -> None:
    """When CLAUDE_SKILLS_DIR contains all prompt files, _build_input picks them up
    and substitutes the symptom into the user prompt template."""
    from sagaflow.prompts import CLAUDE_SKILLS_DIR_ENV

    prompts = tmp_path / "deep-debug" / "prompts"
    prompts.mkdir(parents=True)
    # Write ALL required prompt files so _build_input doesn't raise PromptNotFoundError.
    (prompts / "premortem.system.md").write_text("CUSTOM SYSTEM PROMPT\n")
    (prompts / "premortem.user.md").write_text("CUSTOM USER: $symptom\n")
    for name in [
        "hypothesis.system", "hypothesis.user",
        "outside-frame.system", "outside-frame.user",
        "judge-pass1.system", "judge-pass1.user",
        "judge-pass2.system", "judge-pass2.user",
        "rebuttal.system", "rebuttal.user",
        "probe.system", "probe.user",
        "fix.system", "fix.user",
        "architect.system", "architect.user",
    ]:
        (prompts / f"{name}.md").write_text(f"stub {name}\n")
    monkeypatch.setenv(CLAUDE_SKILLS_DIR_ENV, str(tmp_path))

    inp = _build_input(
        run_id="test-run",
        run_dir="/tmp/test",
        inbox_path="/tmp/INBOX.md",
        cli_args={"symptom": "login breaks when tenant id is null"},
    )

    assert inp.premortem_system_prompt == "CUSTOM SYSTEM PROMPT\n"
    assert inp.premortem_user_prompt == "CUSTOM USER: login breaks when tenant id is null\n"


def test_build_input_raises_when_prompt_file_missing(
    tmp_path, monkeypatch
) -> None:
    """Absent prompt files now raise PromptNotFoundError — setup error, not silent degradation."""
    from sagaflow.prompts import CLAUDE_SKILLS_DIR_ENV, PromptNotFoundError

    monkeypatch.setenv(CLAUDE_SKILLS_DIR_ENV, str(tmp_path))  # empty dir

    with pytest.raises(PromptNotFoundError):
        _build_input(
            run_id="test-run",
            run_dir="/tmp/test",
            inbox_path="/tmp/INBOX.md",
            cli_args={"symptom": "foo"},
        )
