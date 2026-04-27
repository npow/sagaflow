"""Base activities shared by every sagaflow skill."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from temporalio import activity

from sagaflow.inbox import Inbox, InboxEntry
from sagaflow.notify import notify_desktop
from sagaflow.transport.anthropic_sdk import AnthropicSdkTransport, ModelTier
from sagaflow.transport.boundary import validate_boundary, validate_text_boundary
from sagaflow.transport.claude_cli import ClaudeCliTransport
from sagaflow.transport.dispatcher import SubagentRequest, dispatch_subagent
from sagaflow.transport.structured_output import (
    MalformedResponseError,
    parse_structured,
)

logger = logging.getLogger(__name__)

MALFORMED_SENTINEL = "_sagaflow_malformed"
HEARTBEAT_INTERVAL_SECONDS = 20.0


@dataclass(frozen=True)
class WriteArtifactInput:
    path: str
    content: str


@activity.defn(name="write_artifact")
async def write_artifact(inp: WriteArtifactInput) -> None:
    target = Path(inp.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content, boundary = validate_text_boundary(
        inp.content, label=f"write_artifact:{target.name}",
    )
    if boundary.injection_flags:
        logger.warning(
            "Artifact %s has injection flags — logging only, not blocking: %s",
            target.name,
            "; ".join(boundary.injection_flags),
        )
    target.write_text(content, encoding="utf-8")


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
    tools_needed: bool
    max_tokens: int = 128_000
    output_schema: dict | None = None


def _get_sdk() -> AnthropicSdkTransport:
    return AnthropicSdkTransport()


def _get_cli() -> ClaudeCliTransport:
    return ClaudeCliTransport()


async def _heartbeat_loop() -> None:
    """Emit `activity.heartbeat()` every HEARTBEAT_INTERVAL_SECONDS until cancelled.

    Workflows set `heartbeat_timeout` on spawn_subagent to detect hung LLM calls.
    Without an in-activity heartbeat, any LLM response taking longer than
    heartbeat_timeout would false-trip. This loop keeps the activity alive.
    """
    while True:
        try:
            activity.heartbeat()
        except Exception:
            # Activity context may not be set in tests, or heartbeat target may be gone.
            return
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@activity.defn(name="spawn_subagent")
async def spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    prompt_path = Path(inp.user_prompt_path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"subagent input file missing: {prompt_path}")
    user_prompt = prompt_path.read_text(encoding="utf-8")
    if not user_prompt.strip():
        raise FileNotFoundError(f"subagent input file is empty: {prompt_path}")

    _PROMPT_SIZE_WARN = 8192
    if len(user_prompt) > _PROMPT_SIZE_WARN:
        logger.warning(
            "Oversized prompt file (%d bytes, threshold %d) — "
            "content may be inlined instead of referenced by path: %s",
            len(user_prompt),
            _PROMPT_SIZE_WARN,
            prompt_path,
        )

    tier = ModelTier[inp.tier_name]
    sdk = _get_sdk()
    cli = _get_cli()
    label = f"{inp.role}:{prompt_path.stem}"
    request = SubagentRequest(
        role=inp.role,
        tier=tier,
        system_prompt=inp.system_prompt,
        user_prompt=user_prompt,
        max_tokens=inp.max_tokens,
        tools_needed=inp.tools_needed,
        label=label,
        output_schema=inp.output_schema,
    )

    beat_task: asyncio.Task[None] | None = None
    try:
        beat_task = asyncio.create_task(_heartbeat_loop())
    except RuntimeError:
        # No running event loop (shouldn't happen in activity context, but safe).
        beat_task = None

    try:
        raw = await dispatch_subagent(request, sdk_transport=sdk, cli_transport=cli)
    finally:
        if beat_task is not None:
            beat_task.cancel()
            with contextlib.suppress(BaseException):
                await beat_task

    if inp.output_schema is not None:
        import json
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed, _ = validate_boundary(parsed, label=label)
            return parsed
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "Schema-constrained response not valid JSON (label=%s, role=%s, error=%s)",
                label, inp.role, exc,
            )
            return {MALFORMED_SENTINEL: "1", "_error": str(exc), "_raw": raw[:2000]}

    try:
        parsed = parse_structured(raw)
        parsed, _ = validate_boundary(parsed, label=label)
        return parsed
    except MalformedResponseError as exc:
        truncated_raw = raw[:2000] if isinstance(raw, str) else ""
        logger.warning(
            "Malformed subagent response (label=%s, role=%s, error=%s, raw_len=%d): %s",
            label,
            inp.role,
            exc,
            len(raw) if isinstance(raw, str) else 0,
            truncated_raw[:500],
        )
        raw_path = ""
        try:
            dump = prompt_path.with_suffix(".malformed_response")
            dump.write_text(raw if isinstance(raw, str) else "", encoding="utf-8")
            raw_path = str(dump)
        except OSError:
            pass
        return {
            MALFORMED_SENTINEL: "1",
            "_error": str(exc),
            "_raw": truncated_raw,
            "_raw_path": raw_path,
        }
