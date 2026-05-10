"""Import smoke tests — catches missing modules before they reach PyPI.

This file exists because commit 7985de2 added imports for sagaflow.memory.working,
sagaflow.memory.config, and sagaflow.memory.prompt_fragment without committing
those files, breaking the main branch for anyone installing from source.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import sagaflow


def _all_submodules(package) -> list[str]:
    """Walk package tree and return all importable dotted names."""
    names = [package.__name__]
    if not hasattr(package, "__path__"):
        return names
    for info in pkgutil.walk_packages(
        package.__path__, prefix=package.__name__ + "."
    ):
        names.append(info.name)
    return names


class TestAllSubmodulesImport:
    """Every module in the sagaflow package must import without error."""

    SKIP_MODULES = {
        "sagaflow.memory.mcp_server",  # requires `mcp` package at runtime
        "sagaflow.durable",  # requires temporalio
        "sagaflow.durable.activities",
        "sagaflow.durable.claim_check",
        "sagaflow.durable.engine",
        "sagaflow.durable.helpers",
        "sagaflow.durable.signals",
        "sagaflow.durable.worker",
        "sagaflow.durable.workflows",
    }

    @pytest.fixture(scope="class")
    def submodules(self) -> list[str]:
        return _all_submodules(sagaflow)

    def test_discovered_modules(self, submodules: list[str]) -> None:
        assert len(submodules) > 5, f"Expected many submodules, got {submodules}"

    def test_all_import(self, submodules: list[str]) -> None:
        failures = []
        for name in submodules:
            if any(name == skip or name.startswith(skip + ".") for skip in self.SKIP_MODULES):
                continue
            try:
                importlib.import_module(name)
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if failures:
            pytest.fail(
                f"{len(failures)} module(s) failed to import:\n"
                + "\n".join(failures)
            )
