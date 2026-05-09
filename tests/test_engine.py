"""Tests for sagaflow.engine — Pydantic AI dispatch layer."""

from __future__ import annotations

import pytest


def _require_temporal_durable_exec() -> None:
    """Skip when CI's `pip uninstall temporalio` strips the durable_exec deps.

    pydantic_ai.durable_exec.temporal's __init__ imports _logfire which raises
    TypeError (not ModuleNotFoundError) when temporalio is missing, so plain
    importorskip won't catch it.
    """
    try:
        import pydantic_ai.durable_exec.temporal  # noqa: F401
    except (ImportError, TypeError) as exc:
        pytest.skip(f"pydantic-ai temporal durable_exec unavailable: {exc}")


def test_get_sdk_agent_returns_temporal_agent():
    _require_temporal_durable_exec()
    from sagaflow.engine import get_sdk_agent

    agent = get_sdk_agent(
        name="test-critic",
        tier="HAIKU",
        system_prompt="You are a test agent.",
    )
    from pydantic_ai.durable_exec.temporal import TemporalAgent

    assert isinstance(agent, TemporalAgent)


def test_get_sdk_agent_caches_by_name_and_tier():
    _require_temporal_durable_exec()
    from sagaflow.engine import get_sdk_agent, _agent_cache

    _agent_cache.clear()
    a1 = get_sdk_agent(name="cache-test", tier="HAIKU", system_prompt="x")
    a2 = get_sdk_agent(name="cache-test", tier="HAIKU", system_prompt="x")
    assert a1 is a2


def test_get_sdk_agent_different_tiers_are_different():
    _require_temporal_durable_exec()
    from sagaflow.engine import get_sdk_agent, _agent_cache

    _agent_cache.clear()
    a1 = get_sdk_agent(name="tier-test", tier="HAIKU", system_prompt="x")
    a2 = get_sdk_agent(name="tier-test", tier="SONNET", system_prompt="x")
    assert a1 is not a2


def test_tier_to_model_mapping():
    from sagaflow.engine import TIER_TO_MODEL

    assert "HAIKU" in TIER_TO_MODEL
    assert "SONNET" in TIER_TO_MODEL
    assert "OPUS" in TIER_TO_MODEL
    assert all(v.startswith("anthropic:") for v in TIER_TO_MODEL.values())
