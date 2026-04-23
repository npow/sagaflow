"""Skill registration entry point.

Each skill package exposes a `register(registry: SkillRegistry) -> None` function.
The worker imports all registered skills before starting, then hands the
aggregate workflow + activity lists to Temporal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class SkillSpec:
    name: str
    workflow_cls: Any
    activities: list[Callable[..., Any]]
    # build_input(run_id, run_dir, inbox_path, cli_args) -> workflow input dataclass.
    # cli_args is the dict of CLI options (name, path, flags, etc.).
    # Optional to preserve backwards compatibility with existing tests; CLI falls back
    # to a hard-coded hello-world path if build_input is None.
    build_input: Callable[..., Any] | None = None
    # Click options to attach to the `sagaflow launch <skill>` subcommand.
    # Each entry is a (option_name, click.option kwargs) pair. The CLI applies them
    # at dispatch time so each skill declares its own flags.
    cli_options: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


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
        seen: set[str] = set()
        for spec in self._specs.values():
            for act in spec.activities:
                defn = getattr(act, "__temporal_activity_definition", None)
                key = defn.name if defn is not None else f"fn:{id(act)}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(act)
        return out

    def all_workflows(self) -> list[Any]:
        return [spec.workflow_cls for spec in self._specs.values()]
