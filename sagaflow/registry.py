"""Skill registration entry point.

Each skill package exposes a `register(registry: SkillRegistry) -> None` function.
The worker imports all registered skills before starting, then hands the
aggregate workflow + activity lists to Temporal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class SkillSpec:
    name: str
    workflow_cls: Any
    activities: list[Callable[..., Any]]


class SkillRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, SkillSpec] = {}

    def register(self, spec: SkillSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"skill {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> SkillSpec:
        if name not in self._specs:
            raise KeyError(f"unknown skill: {name!r}")
        return self._specs[name]

    def names(self) -> Iterable[str]:
        return list(self._specs.keys())

    def all_activities(self) -> list[Callable[..., Any]]:
        out: list[Callable[..., Any]] = []
        for spec in self._specs.values():
            out.extend(spec.activities)
        return out

    def all_workflows(self) -> list[Any]:
        return [spec.workflow_cls for spec in self._specs.values()]
