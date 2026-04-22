"""Transport dispatcher. Selects SDK vs CLI based on `tools_needed`."""

from __future__ import annotations

from dataclasses import dataclass

from sagaflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier
from sagaflow.transport.claude_cli import ClaudeCliTransport


@dataclass(frozen=True)
class SubagentRequest:
    role: str
    tier: ModelTier
    system_prompt: str
    user_prompt: str
    max_tokens: int
    tools_needed: bool
    cli_timeout_seconds: float = 600.0


async def dispatch_subagent(
    request: SubagentRequest,
    *,
    sdk_transport: AnthropicSdkTransport,
    cli_transport: ClaudeCliTransport,
) -> str:
    if request.tools_needed:
        combined_prompt = f"{request.system_prompt}\n\n---\n\n{request.user_prompt}"
        result = await cli_transport.call(
            prompt=combined_prompt,
            timeout_seconds=request.cli_timeout_seconds,
        )
        return result.stdout

    sdk_result = await sdk_transport.call(
        tier=request.tier,
        system_prompt=request.system_prompt,
        user_prompt=request.user_prompt,
        max_tokens=request.max_tokens,
    )
    return sdk_result.text
