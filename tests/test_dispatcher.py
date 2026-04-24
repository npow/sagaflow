from unittest.mock import AsyncMock

import pytest

from sagaflow.transport.dispatcher import dispatch_subagent, SubagentRequest
from sagaflow.transport.anthropic_sdk import ModelTier, TransportResult
from sagaflow.transport.claude_cli import ClaudeCliResult


@pytest.fixture
def fake_sdk():
    sdk = AsyncMock()
    sdk.call = AsyncMock(
        return_value=TransportResult(text="sdk output", input_tokens=10, output_tokens=5)
    )
    return sdk


@pytest.fixture
def fake_cli():
    cli = AsyncMock()
    cli.call = AsyncMock(
        return_value=ClaudeCliResult(stdout="cli output", stderr="", exit_code=0)
    )
    return cli


async def test_dispatch_uses_sdk_when_no_tools_needed(fake_sdk, fake_cli) -> None:
    result = await dispatch_subagent(
        SubagentRequest(
            role="critic",
            tier=ModelTier.HAIKU,
            system_prompt="s",
            user_prompt="u",
            max_tokens=256,
            tools_needed=False,
        ),
        sdk_transport=fake_sdk,
        cli_transport=fake_cli,
    )
    assert result == "sdk output"
    fake_sdk.call.assert_awaited_once()
    fake_cli.call.assert_not_awaited()


async def test_dispatch_uses_cli_when_tools_needed(fake_sdk, fake_cli) -> None:
    result = await dispatch_subagent(
        SubagentRequest(
            role="critic",
            tier=ModelTier.HAIKU,
            system_prompt="s",
            user_prompt="u",
            max_tokens=256,
            tools_needed=True,
        ),
        sdk_transport=fake_sdk,
        cli_transport=fake_cli,
    )
    assert result == "cli output"
    fake_cli.call.assert_awaited_once()
    fake_sdk.call.assert_not_awaited()


async def test_dispatch_passes_model_and_permissions_to_cli(fake_sdk, fake_cli) -> None:
    await dispatch_subagent(
        SubagentRequest(
            role="researcher",
            tier=ModelTier.SONNET,
            system_prompt="sys",
            user_prompt="user",
            max_tokens=256,
            tools_needed=True,
        ),
        sdk_transport=fake_sdk,
        cli_transport=fake_cli,
    )
    call_kwargs = fake_cli.call.call_args.kwargs
    assert call_kwargs["model"] == "sonnet"
    assert call_kwargs["dangerously_skip_permissions"] is True
