"""deep-debug DRY refactor: prompts loaded from claude-skills repo at build_input time."""

from __future__ import annotations

from skills.deep_debug import _build_input


def test_build_input_loads_premortem_prompts_from_claude_skills_dir(
    tmp_path, monkeypatch
) -> None:
    """When CLAUDE_SKILLS_DIR contains premortem prompts, _build_input picks them up
    and substitutes the symptom into the user prompt template."""
    from sagaflow.prompts import CLAUDE_SKILLS_DIR_ENV

    prompts = tmp_path / "deep-debug" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "premortem.system.md").write_text("CUSTOM SYSTEM PROMPT\n")
    (prompts / "premortem.user.md").write_text("CUSTOM USER: $symptom\n")
    monkeypatch.setenv(CLAUDE_SKILLS_DIR_ENV, str(tmp_path))

    inp = _build_input(
        run_id="test-run",
        run_dir="/tmp/test",
        inbox_path="/tmp/INBOX.md",
        cli_args={"symptom": "login breaks when tenant id is null"},
    )

    assert inp.premortem_system_prompt == "CUSTOM SYSTEM PROMPT\n"
    assert inp.premortem_user_prompt == "CUSTOM USER: login breaks when tenant id is null\n"


def test_build_input_falls_back_to_empty_when_prompt_file_missing(
    tmp_path, monkeypatch
) -> None:
    """Absent prompt files → empty strings on the input. The workflow's inline defaults
    take over via the `or`-fallback in workflow.run(). This lets us migrate prompts
    skill-by-skill without breaking the workflow contract."""
    from sagaflow.prompts import CLAUDE_SKILLS_DIR_ENV

    monkeypatch.setenv(CLAUDE_SKILLS_DIR_ENV, str(tmp_path))  # empty dir

    inp = _build_input(
        run_id="test-run",
        run_dir="/tmp/test",
        inbox_path="/tmp/INBOX.md",
        cli_args={"symptom": "foo"},
    )

    assert inp.premortem_system_prompt == ""
    assert inp.premortem_user_prompt == ""
