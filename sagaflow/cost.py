"""Cost estimation from token counts and model tiers."""

from __future__ import annotations

import logging

from sagaflow.pricing import RATE_CARD

logger = logging.getLogger(__name__)


def estimate_cost(tier: str, input_tokens: int, output_tokens: int) -> float:
    if tier not in RATE_CARD:
        logger.warning("unknown tier %r — cost_usd set to 0.0", tier)
        return 0.0
    r = RATE_CARD[tier]
    return (input_tokens * r["input_per_mtok"] + output_tokens * r["output_per_mtok"]) / 1_000_000
