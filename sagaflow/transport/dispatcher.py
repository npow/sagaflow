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
    label: str = ""
    cli_timeout_seconds: float = 900.0
    output_schema: dict | None = None


@dataclass(frozen=True)
class DispatchResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


_TIER_TO_MODEL_ALIAS: dict[str, str] = {
    "HAIKU": "haiku",
    "SONNET": "sonnet",
    "OPUS": "opus",
}


async def dispatch_subagent(
    request: SubagentRequest,
    *,
    sdk_transport: AnthropicSdkTransport,
    cli_transport: ClaudeCliTransport,
) -> DispatchResult:
    if request.tools_needed:
        combined_prompt = f"{request.system_prompt}\n\n---\n\n{request.user_prompt}"
        model_alias = _TIER_TO_MODEL_ALIAS.get(request.tier.name, "opus")
        result = await cli_transport.call(
            prompt=combined_prompt,
            timeout_seconds=request.cli_timeout_seconds,
            model=model_alias,
            label=request.label,
            dangerously_skip_permissions=True,
        )
        return DispatchResult(text=result.stdout)

    sdk_result = await sdk_transport.call(
        tier=request.tier,
        system_prompt=request.system_prompt,
        user_prompt=request.user_prompt,
        max_tokens=request.max_tokens,
        output_schema=request.output_schema,
    )
    return DispatchResult(
        text=sdk_result.text,
        input_tokens=sdk_result.input_tokens,
        output_tokens=sdk_result.output_tokens,
        model=request.tier.model_id,
    )
