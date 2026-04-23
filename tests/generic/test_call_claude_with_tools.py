from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sagaflow.generic.activities import (
    CallClaudeInput,
    ClaudeResponse,
    ClaudeToolUse,
    call_claude_with_tools,
)


def _fake_response(
    *,
    content: list,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _install_fake_client(response: SimpleNamespace) -> tuple[AsyncMock, object]:
    """Patch `_get_anthropic_client` so `messages.create` returns `response`.

    Returns the `create` AsyncMock (so tests can assert args) and the patch CM
    so the caller can manage its lifecycle.
    """
    create_mock = AsyncMock(return_value=response)
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=create_mock))
    patcher = patch(
        "sagaflow.generic.activities._get_anthropic_client",
        return_value=fake_client,
    )
    return create_mock, patcher


async def test_happy_path_extracts_text_and_tool_uses() -> None:
    content = [
        SimpleNamespace(type="text", text="I'll run a tool."),
        SimpleNamespace(
            type="tool_use",
            id="toolu_123",
            name="read_file_tool",
            input={"path": "foo.txt"},
        ),
        SimpleNamespace(type="text", text=" Done."),
    ]
    response = _fake_response(
        content=content, stop_reason="tool_use", input_tokens=12, output_tokens=7
    )
    create_mock, patcher = _install_fake_client(response)
    with patcher:
        result = await call_claude_with_tools(
            CallClaudeInput(
                system_prompt="you are helpful",
                messages=[{"role": "user", "content": "hi"}],
                tools=[{"name": "read_file_tool", "input_schema": {}}],
                tier_name="HAIKU",
                max_tokens=256,
            )
        )
    assert isinstance(result, ClaudeResponse)
    assert result.text == "I'll run a tool. Done."
    assert result.tool_uses == [
        ClaudeToolUse(id="toolu_123", name="read_file_tool", input={"path": "foo.txt"})
    ]
    assert result.stop_reason == "tool_use"
    assert result.input_tokens == 12
    assert result.output_tokens == 7
    create_mock.assert_awaited_once()


async def test_end_turn_stop_with_text_only() -> None:
    content = [SimpleNamespace(type="text", text="All done.")]
    response = _fake_response(content=content, stop_reason="end_turn")
    _create, patcher = _install_fake_client(response)
    with patcher:
        result = await call_claude_with_tools(
            CallClaudeInput(
                system_prompt="s",
                messages=[{"role": "user", "content": "u"}],
                tools=[],
                tier_name="SONNET",
            )
        )
    assert result.text == "All done."
    assert result.tool_uses == []
    assert result.stop_reason == "end_turn"


async def test_max_tokens_stop_preserves_partial_content() -> None:
    content = [SimpleNamespace(type="text", text="partial answer ...")]
    response = _fake_response(content=content, stop_reason="max_tokens")
    _create, patcher = _install_fake_client(response)
    with patcher:
        result = await call_claude_with_tools(
            CallClaudeInput(
                system_prompt="s",
                messages=[{"role": "user", "content": "u"}],
                tools=[],
                tier_name="OPUS",
                max_tokens=16,
            )
        )
    assert result.text == "partial answer ..."
    assert result.tool_uses == []
    assert result.stop_reason == "max_tokens"


async def test_tools_passed_through_unchanged() -> None:
    content = [SimpleNamespace(type="text", text="ok")]
    response = _fake_response(content=content)
    create_mock, patcher = _install_fake_client(response)
    tools = [
        {"name": "bash_tool", "description": "run bash", "input_schema": {"type": "object"}},
        {"name": "grep_tool", "description": "search", "input_schema": {"type": "object"}},
    ]
    with patcher:
        await call_claude_with_tools(
            CallClaudeInput(
                system_prompt="sys",
                messages=[{"role": "user", "content": "u"}],
                tools=tools,
                tier_name="HAIKU",
                max_tokens=64,
            )
        )
    kwargs = create_mock.call_args.kwargs
    assert kwargs["tools"] == tools
    # System prompt + messages + model + max_tokens are also forwarded.
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "u"}]
    assert kwargs["model"] == "claude-haiku-4-5-20251001"
    assert kwargs["max_tokens"] == 64


async def test_invalid_tier_name_raises_with_clear_message() -> None:
    content = [SimpleNamespace(type="text", text="never called")]
    response = _fake_response(content=content)
    _create, patcher = _install_fake_client(response)
    with patcher:
        with pytest.raises(ValueError, match="invalid tier_name"):
            await call_claude_with_tools(
                CallClaudeInput(
                    system_prompt="s",
                    messages=[{"role": "user", "content": "u"}],
                    tools=[],
                    tier_name="TURBO",  # not a real tier
                )
            )


async def test_heartbeat_loop_is_started_and_cancelled_on_exit() -> None:
    # Make the SDK call yield control so the scheduled heartbeat task gets a tick.
    # Without this yield, AsyncMock completes synchronously and the background
    # heartbeat task may not run before being cancelled.
    import asyncio

    async def _yield_then_respond(*args, **kwargs):
        await asyncio.sleep(0)
        return _fake_response(content=[SimpleNamespace(type="text", text="fast")])

    create_mock = AsyncMock(side_effect=_yield_then_respond)
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=create_mock))
    with patch(
        "sagaflow.generic.activities._get_anthropic_client",
        return_value=fake_client,
    ), patch("sagaflow.durable.activities.activity.heartbeat") as beat:
        result = await call_claude_with_tools(
            CallClaudeInput(
                system_prompt="s",
                messages=[{"role": "user", "content": "u"}],
                tools=[],
                tier_name="HAIKU",
                max_tokens=8,
            )
        )
    assert result.text == "fast"
    # Heartbeat fires immediately on task start, then sleeps 20s. A fast call
    # should see exactly one heartbeat before the loop is cancelled.
    assert beat.call_count >= 1
    assert beat.call_count <= 3
    # The background task was cancelled on exit (no leaked heartbeats after
    # the activity returned).
    pre_exit = beat.call_count
    await asyncio.sleep(0.01)
    assert beat.call_count == pre_exit


async def test_empty_content_returns_empty_text_and_no_tool_uses() -> None:
    response = _fake_response(content=[], stop_reason="end_turn")
    _create, patcher = _install_fake_client(response)
    with patcher:
        result = await call_claude_with_tools(
            CallClaudeInput(
                system_prompt="s",
                messages=[{"role": "user", "content": "u"}],
                tools=[],
                tier_name="HAIKU",
            )
        )
    assert result.text == ""
    assert result.tool_uses == []
