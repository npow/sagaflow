"""Load prompt strings from a skill's ``prompts/`` directory.

Every sagaflow skill may ship a ``prompts/`` subdirectory containing ``.md``
files. Each file is one prompt (system or user). The loader supports simple
``{placeholder}`` substitution so skills can keep runtime-templated prompts in
markdown instead of Python strings.

Design goals:
    - Keep prompts editable as markdown (so non-Python edits don't require code
      changes) and dogfoodable by other surfaces (Claude Code, previews).
    - Load at ``build_input`` time so the prompt travels with the workflow
      input; workflows stay deterministic and don't do file I/O themselves.
    - Fail loudly with the missing key/file when a template references an
      undefined placeholder -- silent fallback hides bugs.
"""

from __future__ import annotations

import os
from pathlib import Path
from string import Template

# Override in tests or to run sagaflow against a different claude-skills checkout.
CLAUDE_SKILLS_DIR_ENV = "CLAUDE_SKILLS_DIR"
_DEFAULT_CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


class PromptNotFoundError(FileNotFoundError):
    """Raised when a prompt file is missing from a skill's prompts directory."""


class PromptTemplateError(KeyError):
    """Raised when a template references a placeholder not provided by the caller."""


def _prompts_dir_for_skill(skill_pkg_file: str) -> Path:
    """Return the ``prompts/`` directory colocated with a skill's ``__init__.py``."""
    return Path(skill_pkg_file).resolve().parent / "prompts"


def claude_skills_dir() -> Path:
    """Return the root of the claude-skills repo (``~/.claude/skills`` by default).

    Override with ``CLAUDE_SKILLS_DIR`` env var. This is the canonical prompt
    source for any skill that exists in both claude-skills and sagaflow (deep-qa,
    deep-debug, deep-research, etc). Editing the ``.md`` file there is the single
    source of truth for both surfaces.
    """
    return Path(os.environ.get(CLAUDE_SKILLS_DIR_ENV, str(_DEFAULT_CLAUDE_SKILLS_DIR)))


def load_prompt(
    skill_pkg_file: str,
    name: str,
    *,
    substitutions: dict[str, str] | None = None,
) -> str:
    """Read ``prompts/<name>.md`` from the skill's package and interpolate.

    Parameters
    ----------
    skill_pkg_file:
        Pass ``__file__`` from the skill's ``__init__.py`` -- used to locate
        the prompts directory.
    name:
        The filename stem (e.g. ``"greeter.system"`` reads ``greeter.system.md``).
    substitutions:
        Optional ``{placeholder}`` → value map. Uses ``string.Template`` so
        placeholders are ``$name`` or ``${name}``; ``{name}``-style braces are
        kept literal. If omitted, the raw file content is returned unchanged.

    Raises
    ------
    PromptNotFoundError:
        The file doesn't exist.
    PromptTemplateError:
        A ``$placeholder`` in the file is missing from ``substitutions``.
    """
    prompt_path = _prompts_dir_for_skill(skill_pkg_file) / f"{name}.md"
    return _read_and_substitute(prompt_path, substitutions)


def load_claude_skill_prompt(
    skill_name: str,
    name: str,
    *,
    substitutions: dict[str, str] | None = None,
) -> str:
    """Read ``~/.claude/skills/<skill_name>/prompts/<name>.md`` and interpolate.

    Use this for skills that live in the claude-skills repo. Editing the markdown
    file there is the canonical source for both the Claude Code driver and the
    sagaflow workflow — no duplication.
    """
    prompt_path = claude_skills_dir() / skill_name / "prompts" / f"{name}.md"
    return _read_and_substitute(prompt_path, substitutions)


def _read_and_substitute(
    prompt_path: Path, substitutions: dict[str, str] | None
) -> str:
    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptNotFoundError(
            f"prompt file not found: {prompt_path} (cwd={Path.cwd()})"
        ) from exc
    if not substitutions:
        return raw
    try:
        return Template(raw).substitute(substitutions)
    except KeyError as exc:
        raise PromptTemplateError(
            f"prompt {prompt_path} references ${{{exc.args[0]}}} but no value was provided "
            f"(available: {sorted(substitutions)})"
        ) from exc
