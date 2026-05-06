"""Legacy skill adapter — wraps existing workflow.py with prompt auto-discovery."""

from __future__ import annotations

from pathlib import Path

from sagaflow.manifest.prompts import PromptRegistry


class LegacySkillAdapter:
    """Wraps an existing workflow.py skill class, injecting PromptRegistry.

    The legacy _build_input() continues to work; skills can opt into
    auto-discovery incrementally by reading self._prompt_registry.
    """

    def __init__(self, workflow_class: type, skill_root: Path) -> None:
        self.workflow_class = workflow_class
        self.prompts = PromptRegistry(skill_root)

    def build_input_with_prompts(self, **kwargs: object) -> dict[str, object]:
        """Return all prompts as a dict, ready to merge into an Input dataclass."""
        return {
            path.replace("/", "_").replace(".", "_").removesuffix("_md"): content
            for path, content in sorted(self.prompts._cache.items())
        }
