"""Pydantic AI engine — dispatch layer replacing transport/dispatcher.

SDK path: non-tool-using LLM calls go through pydantic_ai.Agent wrapped
in TemporalAgent. Activities are auto-registered via PydanticAIPlugin.

CLI path: tool-using agents use ClaudeCliTransport (unchanged).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalAgent
from temporalio.workflow import ActivityConfig

logger = logging.getLogger(__name__)

TIER_TO_MODEL: dict[str, str] = {
    "HAIKU": "anthropic:claude-haiku-4-5-20251001",
    "SONNET": "anthropic:claude-sonnet-4-6",
    "OPUS": "anthropic:claude-opus-4-7",
}

_DEFAULT_TIMEOUT = timedelta(minutes=15)

_agent_cache: dict[str, TemporalAgent[None, Any]] = {}


def _cache_key(name: str, tier: str, system_prompt: str) -> str:
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:12]
    return f"{name}:{tier}:{prompt_hash}"


def get_sdk_agent(
    name: str,
    tier: str,
    system_prompt: str,
    output_type: type[BaseModel] | None = None,
    max_tokens: int = 128_000,
    timeout: timedelta = _DEFAULT_TIMEOUT,
) -> TemporalAgent[None, Any]:
    key = _cache_key(name, tier, system_prompt)
    if key in _agent_cache:
        return _agent_cache[key]

    model = TIER_TO_MODEL.get(tier, TIER_TO_MODEL["SONNET"])
    agent = Agent(
        model,
        name=name,
        instructions=system_prompt,
        output_type=output_type or str,
    )
    temporal_agent = TemporalAgent(
        agent,
        name=name,
        activity_config=ActivityConfig(
            start_to_close_timeout=timeout,
        ),
    )
    _agent_cache[key] = temporal_agent
    return temporal_agent


def all_cached_agents() -> list[TemporalAgent[None, Any]]:
    return list(_agent_cache.values())
