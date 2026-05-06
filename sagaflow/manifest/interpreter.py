"""In-session interpreter for manifest-driven skills.

Drives manifest execution without Temporal — used by Claude Code
for interactive skill runs when sagaflow is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sagaflow.manifest.executor import ManifestExecutor
from sagaflow.manifest.temporal import find_skill_root, load_manifest


class ManifestInterpreter:

    def __init__(self, skill_root: Path, client: Any | None = None) -> None:
        self.skill_root = skill_root
        self._client = client
        self.manifest = load_manifest(skill_root)
        self.executor = ManifestExecutor(
            manifest=self.manifest,
            skill_root=skill_root,
            model_call=self._model_call,
            dispatch_call=self._dispatch_call,
        )

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic()
        return self._client

    async def run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return await self.executor.execute(inputs)

    async def _model_call(self, system: str, prompt: str, opts: dict[str, Any]) -> str:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": opts.get("model", "claude-sonnet-4-6"),
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        response = await client.messages.create(**kwargs)
        return response.content[0].text

    async def _dispatch_call(self, skill: str, inputs: dict[str, Any]) -> Any:
        child_root = find_skill_root(skill)
        child = ManifestInterpreter(child_root, self._client)
        return await child.run(inputs)
