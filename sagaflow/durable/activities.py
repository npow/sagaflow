"""Base activities shared by every sagaflow skill."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from temporalio import activity

from sagaflow.inbox import Inbox, InboxEntry
from sagaflow.notify import notify_desktop
from sagaflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier
from sagaflow.transport.claude_cli import ClaudeCliTransport
from sagaflow.transport.dispatcher import SubagentRequest, dispatch_subagent
from sagaflow.transport.structured_output import parse_structured


@dataclass(frozen=True)
class WriteArtifactInput:
    path: str
    content: str


@activity.defn(name="write_artifact")
async def write_artifact(inp: WriteArtifactInput) -> None:
    target = Path(inp.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(inp.content, encoding="utf-8")


@dataclass(frozen=True)
class EmitFindingInput:
    inbox_path: str
    run_id: str
    skill: str
    status: str
    summary: str
    notify: bool
    timestamp_iso: str


@activity.defn(name="emit_finding")
async def emit_finding(inp: EmitFindingInput) -> None:
    inbox = Inbox(path=Path(inp.inbox_path))
    inbox.append(
        InboxEntry(
            run_id=inp.run_id,
            skill=inp.skill,
            status=inp.status,
            summary=inp.summary,
            timestamp=datetime.fromisoformat(inp.timestamp_iso),
        )
    )
    if inp.notify:
        notify_desktop(
            title=f"sagaflow: {inp.run_id} {inp.status}",
            body=inp.summary or inp.skill,
        )


@dataclass(frozen=True)
class SpawnSubagentInput:
    role: str
    tier_name: str                # ModelTier.name — pydantic-safe string
    system_prompt: str
    user_prompt_path: str
    max_tokens: int
    tools_needed: bool


def _get_sdk() -> AnthropicSdkTransport:
    return AnthropicSdkTransport()


def _get_cli() -> ClaudeCliTransport:
    return ClaudeCliTransport()


@activity.defn(name="spawn_subagent")
async def spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    prompt_path = Path(inp.user_prompt_path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"subagent input file missing: {prompt_path}")
    user_prompt = prompt_path.read_text(encoding="utf-8")
    if not user_prompt.strip():
        raise FileNotFoundError(f"subagent input file is empty: {prompt_path}")

    tier = ModelTier[inp.tier_name]
    sdk = _get_sdk()
    cli = _get_cli()
    request = SubagentRequest(
        role=inp.role,
        tier=tier,
        system_prompt=inp.system_prompt,
        user_prompt=user_prompt,
        max_tokens=inp.max_tokens,
        tools_needed=inp.tools_needed,
    )
    raw = await dispatch_subagent(request, sdk_transport=sdk, cli_transport=cli)
    return parse_structured(raw)
