"""Scenario metadata registry with ``@scenario`` decorator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

SCENARIO_REGISTRY: dict[str, "ScenarioMeta"] = {}


@dataclass
class ScenarioMeta:
    """Metadata for a registered scenario test."""
    name: str
    skill: str
    traces_bug: str
    failure_modes: list[str]
    tags: list[str]
    func: Callable


def scenario(
    skill: str,
    traces_bug: str = "",
    failure_modes: list[str] | None = None,
    tags: list[str] | None = None,
):
    """Decorator that registers a test function in ``SCENARIO_REGISTRY``."""

    def decorator(func: Callable) -> Callable:
        key = f"{skill}::{func.__name__}"
        SCENARIO_REGISTRY[key] = ScenarioMeta(
            name=func.__name__,
            skill=skill,
            traces_bug=traces_bug,
            failure_modes=failure_modes or [],
            tags=tags or [],
            func=func,
        )
        return func

    return decorator
