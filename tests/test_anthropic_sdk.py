from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from sagaflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier, _MAX_DELAY_S


def _make_stream_context(text="hello back", input_tokens=5, output_tokens=2):
    final_message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    ctx.get_final_message = AsyncMock(return_value=final_message)
    return ctx


@pytest.fixture
def mock_anthropic_client(monkeypatch):
    stream_ctx = _make_stream_context()
    stream_fn = MagicMock(return_value=stream_ctx)
    fake = SimpleNamespace(
        messages=SimpleNamespace(stream=stream_fn)
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
    call_args = mock_anthropic_client.messages.stream.call_args.kwargs
    assert call_args["model"] == "claude-sonnet-4-6"
    assert call_args["max_tokens"] == 32


def test_tier_model_ids_are_pinned() -> None:
    assert ModelTier.HAIKU.model_id == "claude-haiku-4-5-20251001"
    assert ModelTier.SONNET.model_id == "claude-sonnet-4-6"
    assert ModelTier.OPUS.model_id == "claude-opus-4-7"


def _make_api_status_error(status_code: int) -> anthropic.APIStatusError:
    response = httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://x"))
    return anthropic.APIStatusError(
        message=f"Error {status_code}",
        response=response,
        body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )


@patch("sagaflow.transport.anthropic_sdk.asyncio.sleep", new_callable=AsyncMock)
async def test_retries_on_overloaded_then_succeeds(mock_sleep) -> None:
    success_ctx = _make_stream_context(text="recovered")
    fail_error = _make_api_status_error(529)

    call_count = 0

    def stream_side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise fail_error
        return success_ctx

    client = SimpleNamespace(messages=SimpleNamespace(stream=MagicMock(side_effect=stream_side_effect)))
    transport = AnthropicSdkTransport(client=client)
    result = await transport.call(
        tier=ModelTier.HAIKU, system_prompt="s", user_prompt="u", max_tokens=16,
    )
    assert result.text == "recovered"
    assert call_count == 3
    assert mock_sleep.call_count == 2


@patch("sagaflow.transport.anthropic_sdk.asyncio.sleep", new_callable=AsyncMock)
async def test_retries_indefinitely_for_529_until_success(mock_sleep) -> None:
    """529 retries have no count ceiling — they keep going until the API recovers."""
    success_ctx = _make_stream_context(text="finally")
    fail_error = _make_api_status_error(529)
    failures_before_success = 20

    call_count = 0

    def stream_side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= failures_before_success:
            raise fail_error
        return success_ctx

    client = SimpleNamespace(messages=SimpleNamespace(stream=MagicMock(side_effect=stream_side_effect)))
    transport = AnthropicSdkTransport(client=client)
    result = await transport.call(
        tier=ModelTier.HAIKU, system_prompt="s", user_prompt="u", max_tokens=16,
    )
    assert result.text == "finally"
    assert call_count == failures_before_success + 1
    assert mock_sleep.call_count == failures_before_success


@patch("sagaflow.transport.anthropic_sdk.asyncio.sleep", new_callable=AsyncMock)
async def test_backoff_caps_at_max_delay(mock_sleep) -> None:
    """Exponential backoff should cap at _MAX_DELAY_S, not grow unbounded."""
    success_ctx = _make_stream_context(text="ok")
    fail_error = _make_api_status_error(529)
    num_failures = 15  # enough to hit the cap

    call_count = 0

    def stream_side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= num_failures:
            raise fail_error
        return success_ctx

    client = SimpleNamespace(messages=SimpleNamespace(stream=MagicMock(side_effect=stream_side_effect)))
    transport = AnthropicSdkTransport(client=client)
    await transport.call(
        tier=ModelTier.HAIKU, system_prompt="s", user_prompt="u", max_tokens=16,
    )
    delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert all(d <= _MAX_DELAY_S for d in delays), f"delay exceeded cap: {delays}"
    assert delays[-1] == _MAX_DELAY_S, f"last delay should be capped: {delays[-1]}"


async def test_non_retryable_error_raises_immediately() -> None:
    fail_error = _make_api_status_error(400)
    client = SimpleNamespace(
        messages=SimpleNamespace(stream=MagicMock(side_effect=fail_error))
    )
    transport = AnthropicSdkTransport(client=client)
    with pytest.raises(anthropic.APIStatusError):
        await transport.call(
            tier=ModelTier.HAIKU, system_prompt="s", user_prompt="u", max_tokens=16,
        )
    assert client.messages.stream.call_count == 1


@patch("sagaflow.transport.anthropic_sdk.asyncio.sleep", new_callable=AsyncMock)
async def test_all_retryable_status_codes_are_retried(mock_sleep) -> None:
    """429, 500, 502, 503, 529 should all trigger retry."""
    for code in (429, 500, 502, 503, 529):
        success_ctx = _make_stream_context(text="ok")
        fail_error = _make_api_status_error(code)
        call_count = 0

        def stream_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise fail_error
            return success_ctx

        client = SimpleNamespace(messages=SimpleNamespace(stream=MagicMock(side_effect=stream_side_effect)))
        transport = AnthropicSdkTransport(client=client)
        result = await transport.call(
            tier=ModelTier.HAIKU, system_prompt="s", user_prompt="u", max_tokens=16,
        )
        assert result.text == "ok", f"status {code} should have been retried"
        assert call_count == 2, f"status {code}: expected 2 calls, got {call_count}"
        mock_sleep.reset_mock()
