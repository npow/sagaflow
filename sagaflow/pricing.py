"""Static rate card for LLM cost estimation."""

from __future__ import annotations

# Anthropic public list prices (USD per million tokens). Cache-creation
# is +25% of input; cache-read is 10% of input. Override via env or a
# pricing-overrides file if your contract differs from list.
RATE_CARD: dict[str, dict[str, float]] = {
    "SONNET": {
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_creation_per_mtok": 3.75,
        "cache_read_per_mtok": 0.30,
    },
    "HAIKU": {
        "input_per_mtok": 0.80,
        "output_per_mtok": 4.00,
        "cache_creation_per_mtok": 1.00,
        "cache_read_per_mtok": 0.08,
    },
    "OPUS": {
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
        "cache_creation_per_mtok": 18.75,
        "cache_read_per_mtok": 1.50,
    },
}
