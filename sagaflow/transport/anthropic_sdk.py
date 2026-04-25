"""Anthropic-SDK transport. Default subagent backend."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum

import anthropic
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}
_MAX_RETRIES = 4
_BASE_DELAY_S = 2.0


class ModelTier(Enum):
    HAIKU = "claude-haiku-4-5-20251001"
    SONNET = "claude-sonnet-4-6"
    OPUS = "claude-opus-4-7"

    @property
    def model_id(self) -> str:
        return self.value


@dataclass
class TransportResult:
    text: str
    input_tokens: int
    output_tokens: int


class AnthropicSdkTransport:
    """Call the Anthropic API. Honors ANTHROPIC_BASE_URL override (e.g., model gateway)."""

    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        if client is None:
            base_url = os.environ.get("ANTHROPIC_BASE_URL")
            api_key = os.environ.get("ANTHROPIC_API_KEY", "sk-dummy")
            client = AsyncAnthropic(base_url=base_url, api_key=api_key)
        self._client = client

    async def call(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> TransportResult:
        last_exc: anthropic.APIStatusError | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client.messages.stream(
                    model=tier.model_id,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                ) as stream:
                    response = await stream.get_final_message()
                text_parts = [
                    block.text for block in response.content if block.type == "text"
                ]
                return TransportResult(
                    text="".join(text_parts),
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
            except anthropic.APIStatusError as exc:
                if exc.status_code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_RETRIES:
                    raise
                last_exc = exc
                delay = _BASE_DELAY_S * (2 ** attempt)
                logger.warning(
                    "Anthropic API %d (attempt %d/%d), retrying in %.0fs",
                    exc.status_code,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None  # unreachable; satisfies type checker
        raise last_exc
