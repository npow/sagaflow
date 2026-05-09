"""Cost estimation from token counts and model tiers."""

from __future__ import annotations

import logging
from typing import Any

from sagaflow.pricing import RATE_CARD

logger = logging.getLogger(__name__)


def estimate_cost(
    tier: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    if tier not in RATE_CARD:
        logger.warning("unknown tier %r — cost_usd set to 0.0", tier)
        return 0.0
    r = RATE_CARD[tier]
    cc_rate = r.get("cache_creation_per_mtok", r["input_per_mtok"] * 1.25)
    cr_rate = r.get("cache_read_per_mtok", r["input_per_mtok"] * 0.10)
    return (
        input_tokens * r["input_per_mtok"]
        + output_tokens * r["output_per_mtok"]
        + cache_creation_tokens * cc_rate
        + cache_read_tokens * cr_rate
    ) / 1_000_000


def _infer_tier(model_name: str) -> str:
    m = model_name.lower()
    if "haiku" in m:
        return "HAIKU"
    if "opus" in m:
        return "OPUS"
    return "SONNET"


def estimate_cost_from_result(result: dict[str, Any]) -> float:
    """Extract token counts and model from a spawn_subagent result dict and estimate cost.

    Reads the four token fields recorded by ``spawn_subagent`` separately:
    ``_input_tokens`` (regular, full-rate input), ``_output_tokens``,
    ``_cache_creation_tokens``, ``_cache_read_tokens``. Older results that
    only have ``_input_tokens`` (with cache totals folded in) still parse —
    they'll over-estimate cost, but they don't error.
    """
    try:
        input_tokens = int(result.get("_input_tokens", 0))
        output_tokens = int(result.get("_output_tokens", 0))
        cache_creation_tokens = int(result.get("_cache_creation_tokens", 0))
        cache_read_tokens = int(result.get("_cache_read_tokens", 0))
    except (TypeError, ValueError):
        return 0.0
    model = result.get("_model", "")
    tier = _infer_tier(model) if model else "SONNET"
    return estimate_cost(tier, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)
