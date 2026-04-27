"""Static rate card for LLM cost estimation."""

from __future__ import annotations

RATE_CARD: dict[str, dict[str, float]] = {
    "SONNET": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "HAIKU": {"input_per_mtok": 0.80, "output_per_mtok": 4.00},
    "OPUS": {"input_per_mtok": 15.00, "output_per_mtok": 75.00},
}
