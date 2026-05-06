"""Mutable state bag threaded through step execution.

API contract:
- get(path): accepts dot-separated paths for nested dict lookup.
- set(path, value): accepts dot-separated keys. Intermediate dicts created.
"""

from __future__ import annotations

import copy
from typing import Any


class ExecutionContext:

    def __init__(self, inputs: dict[str, Any]) -> None:
        self._data: dict[str, Any] = {"inputs": inputs}

    def get(self, path: str) -> Any:
        parts = path.split(".")
        current: Any = self._data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        target = self._data
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value

    def resolve_map(self, mapping: dict[str, str]) -> dict[str, Any]:
        return {k: self.get(v) for k, v in mapping.items()}

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def restore(self, snapshot: dict[str, Any]) -> None:
        self._data = snapshot

    def branch(self) -> ExecutionContext:
        child = ExecutionContext({})
        child._data = self.snapshot()
        return child
