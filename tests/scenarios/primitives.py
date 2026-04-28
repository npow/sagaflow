"""Composable failure injection primitives for scenario reliability testing.

Each primitive produces a deterministic failure response mimicking a specific
LLM/infrastructure failure mode.  ``make_scenario_fake`` wraps a happy-path
base fake with failure injections declared via ``ScenarioConfig``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from temporalio import activity

from sagaflow.durable.activities import MALFORMED_SENTINEL, SpawnSubagentInput

_TOKEN_META = {
    "_input_tokens": "100",
    "_output_tokens": "50",
    "_model": "scenario-fake",
}

BaseFake = Callable[[SpawnSubagentInput], Awaitable[dict[str, str]]]


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PartialQuorumSpec:
    """Which roles to fail and how many to keep succeeding."""
    fail_roles: list[str]
    keep_count: int = 1


@dataclass
class ScenarioConfig:
    """Declarative failure-injection specification."""
    malformed_roles: list[str] = field(default_factory=list)
    truncated_roles: list[str] = field(default_factory=list)
    empty_roles: list[str] = field(default_factory=list)
    timeout_roles: list[str] = field(default_factory=list)
    model_error_roles: list[str] = field(default_factory=list)
    partial_quorum: PartialQuorumSpec | None = None
    oversized_input: bool = False
    garbage_input_roles: list[str] = field(default_factory=list)
    conflict_pairs: list[tuple[str, str]] = field(default_factory=list)
    duplicate_defect_roles: list[str] = field(default_factory=list)
    conflicting_verdicts_roles: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Primitive response generators
# ---------------------------------------------------------------------------

def malformed_response(role: str) -> dict[str, str]:
    """MALFORMED_SENTINEL response — parser couldn't extract structured output."""
    return {
        MALFORMED_SENTINEL: "1",
        "_error": f"simulated malformed response for {role}",
        "_raw": f"garbled output for {role}",
        **_TOKEN_META,
    }


def truncated_response(
    base_result: dict[str, str],
    fraction: float = 0.3,
) -> dict[str, str]:
    """Truncate string values to simulate output cutoff."""
    result = {}
    for k, v in base_result.items():
        if k.startswith("_"):
            result[k] = v
        else:
            cut = max(1, int(len(v) * fraction))
            result[k] = v[:cut]
    return result


def empty_response(role: str) -> dict[str, str]:
    """Completely empty response — no structured output."""
    return {
        MALFORMED_SENTINEL: "1",
        "_error": f"empty response for {role}",
        "_raw": "",
        **_TOKEN_META,
    }


def model_error_response(role: str) -> dict[str, str]:
    """API-level model error (overloaded, rate-limited)."""
    return {
        MALFORMED_SENTINEL: "1",
        "_error": f"overloaded_error for {role}",
        "_raw": '{"type":"error","error":{"type":"overloaded_error"}}',
        **_TOKEN_META,
    }


def garbage_response(role: str) -> dict[str, str]:
    """Random nonsensical content — not even close to expected schema."""
    return {
        MALFORMED_SENTINEL: "1",
        "_error": f"garbage input for {role}",
        "_raw": "lorem ipsum dolor sit amet " * 20,
        **_TOKEN_META,
    }


def duplicate_defect_response(
    base_result: dict[str, str],
    key: str = "DEFECTS",
) -> dict[str, str]:
    """Duplicate items in a JSON array field."""
    result = dict(base_result)
    if key in result:
        try:
            items = json.loads(result[key])
            if isinstance(items, list) and items:
                result[key] = json.dumps(items + items)
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def conflicting_verdicts_response(role: str) -> dict[str, str]:
    """Internally contradictory verdicts."""
    return {
        "VERDICTS": json.dumps([
            {
                "defect_id": "d1",
                "severity": "critical",
                "confidence": "high",
                "calibration": "confirm",
                "rationale": "Clearly a bug.",
            },
            {
                "defect_id": "d1",
                "severity": "cosmetic",
                "confidence": "high",
                "calibration": "downgrade",
                "rationale": "Not actually a problem.",
            },
        ]),
        **_TOKEN_META,
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_scenario_fake(
    base_fake: BaseFake,
    config: ScenarioConfig,
) -> BaseFake:
    """Wrap a base fake with failure injections from *config*.

    Returns a Temporal activity that can replace the base fake in a Worker's
    activities list.  Calls that don't match any injection rule fall through
    to *base_fake*.
    """
    call_counts: dict[str, int] = {}

    @activity.defn(name="spawn_subagent")
    async def scenario_fake(inp: SpawnSubagentInput) -> dict[str, str]:
        role = inp.role

        if role in config.malformed_roles:
            return malformed_response(role)

        if role in config.empty_roles:
            return empty_response(role)

        if role in config.timeout_roles:
            raise asyncio.TimeoutError(f"simulated timeout for {role}")

        if role in config.model_error_roles:
            return model_error_response(role)

        if role in config.garbage_input_roles:
            return garbage_response(role)

        if role in config.conflicting_verdicts_roles:
            return conflicting_verdicts_response(role)

        # Partial quorum — fail after keep_count successes for specified roles
        if config.partial_quorum and role in config.partial_quorum.fail_roles:
            call_counts[role] = call_counts.get(role, 0) + 1
            if call_counts[role] > config.partial_quorum.keep_count:
                return malformed_response(role)

        # Get base result for transforms that modify happy-path output
        base_result = await base_fake(inp)

        if role in config.truncated_roles:
            return truncated_response(base_result)

        if role in config.duplicate_defect_roles:
            return duplicate_defect_response(base_result)

        return base_result

    return scenario_fake
