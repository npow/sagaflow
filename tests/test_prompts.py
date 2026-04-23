"""Tests for the prompt-loader utility that lets skills keep prompts in .md files."""

from __future__ import annotations

import pytest

from sagaflow.prompts import (
    PromptNotFoundError,
    PromptTemplateError,
    load_prompt,
)


def _write_prompt(tmp_path, name: str, body: str) -> str:
    pkg_file = tmp_path / "__init__.py"
    pkg_file.write_text("")
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / f"{name}.md").write_text(body, encoding="utf-8")
    return str(pkg_file)


def test_load_prompt_returns_file_contents_verbatim(tmp_path) -> None:
    pkg_file = _write_prompt(tmp_path, "system", "You are a greeter.\n")
    assert load_prompt(pkg_file, "system") == "You are a greeter.\n"


def test_load_prompt_substitutes_template_variables(tmp_path) -> None:
    pkg_file = _write_prompt(tmp_path, "user", "Greet $name on $day.\n")
    out = load_prompt(pkg_file, "user", substitutions={"name": "alice", "day": "Monday"})
    assert out == "Greet alice on Monday.\n"


def test_load_prompt_supports_brace_form(tmp_path) -> None:
    pkg_file = _write_prompt(tmp_path, "user", "Hello ${name}!\n")
    assert load_prompt(pkg_file, "user", substitutions={"name": "world"}) == "Hello world!\n"


def test_load_prompt_preserves_literal_curly_braces(tmp_path) -> None:
    # string.Template only substitutes $-prefixed; `{foo}` should be preserved so prompts
    # can include JSON examples without escaping.
    pkg_file = _write_prompt(tmp_path, "p", '{"example": "value"}\nGreet $name\n')
    out = load_prompt(pkg_file, "p", substitutions={"name": "alice"})
    assert out == '{"example": "value"}\nGreet alice\n'


def test_load_prompt_missing_file_raises(tmp_path) -> None:
    (tmp_path / "__init__.py").write_text("")
    with pytest.raises(PromptNotFoundError):
        load_prompt(str(tmp_path / "__init__.py"), "does-not-exist")


def test_load_prompt_missing_placeholder_raises(tmp_path) -> None:
    pkg_file = _write_prompt(tmp_path, "p", "Greet $name and $unknown\n")
    with pytest.raises(PromptTemplateError, match="unknown"):
        load_prompt(pkg_file, "p", substitutions={"name": "alice"})


def test_load_prompt_dollar_escape(tmp_path) -> None:
    pkg_file = _write_prompt(tmp_path, "p", "Price is $$5 for $name\n")
    out = load_prompt(pkg_file, "p", substitutions={"name": "alice"})
    assert out == "Price is $5 for alice\n"


def test_load_claude_skill_prompt_reads_from_claude_skills_dir(tmp_path, monkeypatch) -> None:
    from sagaflow.prompts import CLAUDE_SKILLS_DIR_ENV, load_claude_skill_prompt

    monkeypatch.setenv(CLAUDE_SKILLS_DIR_ENV, str(tmp_path))
    skill_dir = tmp_path / "my-skill" / "prompts"
    skill_dir.mkdir(parents=True)
    (skill_dir / "critic.system.md").write_text("You are a critic for $domain.\n")

    out = load_claude_skill_prompt(
        "my-skill", "critic.system", substitutions={"domain": "code"}
    )
    assert out == "You are a critic for code.\n"


def test_load_claude_skill_prompt_missing_file_points_to_claude_skills_dir(
    tmp_path, monkeypatch
) -> None:
    from sagaflow.prompts import CLAUDE_SKILLS_DIR_ENV, load_claude_skill_prompt

    monkeypatch.setenv(CLAUDE_SKILLS_DIR_ENV, str(tmp_path))
    with pytest.raises(PromptNotFoundError, match=str(tmp_path)):
        load_claude_skill_prompt("my-skill", "missing")
