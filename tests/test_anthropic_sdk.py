from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sagaflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier


@pytest.fixture
def mock_anthropic_client(monkeypatch):
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="hello back")],
                    usage=SimpleNamespace(input_tokens=5, output_tokens=2),
                )
            )
        )
    )
    return fake


async def test_call_returns_text_and_usage(mock_anthropic_client) -> None:
    transport = AnthropicSdkTransport(client=mock_anthropic_client)
    result = await transport.call(
        tier=ModelTier.HAIKU,
        system_prompt="be brief",
        user_prompt="say hi",
        max_tokens=16,
    )
    assert result.text == "hello back"
    assert result.input_tokens == 5
    assert result.output_tokens == 2


async def test_call_forwards_model_id_for_tier(mock_anthropic_client) -> None:
    transport = AnthropicSdkTransport(client=mock_anthropic_client)
    await transport.call(
        tier=ModelTier.SONNET,
        system_prompt="s",
        user_prompt="u",
        max_tokens=32,
    )
    call_args = mock_anthropic_client.messages.create.call_args.kwargs
    assert call_args["model"] == "claude-sonnet-4-6"
    assert call_args["max_tokens"] == 32


def test_tier_model_ids_are_pinned() -> None:
    assert ModelTier.HAIKU.model_id == "claude-haiku-4-5-20251001"
    assert ModelTier.SONNET.model_id == "claude-sonnet-4-6"
    assert ModelTier.OPUS.model_id == "claude-opus-4-7"
