"""Auto-discovers all .md files under skill_root/prompts/."""

from __future__ import annotations

import hashlib
from pathlib import Path
from string import Template
from typing import Any


class PromptRegistry:

    def __init__(self, skill_root: Path) -> None:
        self.skill_root = skill_root
        self._cache: dict[str, str] = {}
        self._scan()

    def _scan(self) -> None:
        prompts_dir = self.skill_root / "prompts"
        if not prompts_dir.exists():
            return
        for path in sorted(prompts_dir.rglob("*.md")):
            key = str(path.relative_to(self.skill_root))
            self._cache[key] = path.read_text()

    def get(self, rel_path: str, context: dict[str, Any] | None = None, *, optional: bool = False) -> str:
        raw = self._cache.get(rel_path)
        if raw is None:
            if optional:
                return ""
            available = sorted(self._cache)
            raise KeyError(
                f"Prompt not found: {rel_path!r} in {self.skill_root} "
                f"(available: {available})"
            )
        if context:
            return self._render(raw, context)
        return raw

    def _render(self, template: str, context: dict[str, Any]) -> str:
        flat: dict[str, str] = {}
        for k, v in context.items():
            if isinstance(v, str):
                flat[k] = v
            elif v is not None:
                flat[k] = str(v)
        return Template(template).safe_substitute(flat)

    def sha(self, rel_path: str) -> str:
        content = self._cache.get(rel_path, "")
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def all_paths(self) -> list[str]:
        return sorted(self._cache)
