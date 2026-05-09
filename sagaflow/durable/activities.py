"""Base activities shared by every sagaflow skill."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from temporalio import activity

from sagaflow.durable.claim_check import spill_large_values
from sagaflow.inbox import Inbox, InboxEntry
from sagaflow.notify import notify_desktop
from sagaflow.transport.boundary import validate_boundary, validate_text_boundary
from sagaflow.transport.claude_cli import ClaudeCliTransport

logger = logging.getLogger(__name__)

MALFORMED_SENTINEL = "_sagaflow_malformed"
HEARTBEAT_INTERVAL_SECONDS = 20.0


@dataclass(frozen=True)
class WriteArtifactInput:
    path: str
    content: str
    append: bool = False


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
    if inp.append and target.exists():
        with target.open("a", encoding="utf-8") as f:
            f.write(content)
    else:
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
    tier_name: str
    system_prompt: str
    user_prompt_path: str
    tools_needed: bool
    max_tokens: int = 128_000
    output_schema: dict | None = None
    run_dir: str = ""
    step_index: int = 0
    mcp_config_path: str | None = None
    cli_timeout_seconds: float = 3600.0
    # If set, the activity uses this inline content as the user prompt and
    # ignores ``user_prompt_path`` for reading. ``user_prompt_path`` is still
    # used as a label for logging. Inline prompts avoid the file-write +
    # file-read pair across activities, which is fragile in multi-host
    # deployments where filesystem isn't shared.
    user_prompt: str | None = None
    enable_working_memory: bool = False


_cli_singleton: ClaudeCliTransport | None = None


def _get_cli() -> ClaudeCliTransport:
    global _cli_singleton
    if _cli_singleton is None:
        _cli_singleton = ClaudeCliTransport()
    return _cli_singleton


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


def _extract_json_object(raw: str) -> dict | None:
    """Best-effort extraction of a JSON object from potentially wrapped model output.

    Tries, in order: direct parse, markdown code-block extraction,
    STRUCTURED_OUTPUT_START/END KEY|VALUE block, brace-delimited substring.
    Returns the parsed dict, or None if all attempts fail.
    """
    import json
    import re

    if not raw or not raw.strip():
        return None

    text = raw.strip()

    # 1. Direct parse (happy path — model returned clean JSON).
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. Extract from markdown code blocks: ```json ... ``` or ``` ... ```
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if code_block:
        try:
            obj = json.loads(code_block.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. STRUCTURED_OUTPUT_START/END block with KEY|VALUE lines.
    #    Checked before brace-matching to avoid grabbing JSON fragments
    #    embedded inside values (e.g. CLAIMS arrays).
    #    Values can span multiple lines — accumulate until the next KEY| line.
    block = re.search(
        r"STRUCTURED_OUTPUT_START\s*\n(.*?)\nSTRUCTURED_OUTPUT_END",
        text,
        re.DOTALL,
    )
    if block:
        result: dict[str, str] = {}
        current_key: str | None = None
        current_lines: list[str] = []
        for line in block.group(1).splitlines():
            parts = line.split("|", 1)
            candidate_key = parts[0].strip() if len(parts) == 2 else ""
            if candidate_key and re.match(r"^[A-Z][A-Z0-9_-]*$", candidate_key):
                if current_key:
                    result[current_key] = "\n".join(current_lines).strip()
                current_key = candidate_key
                current_lines = [parts[1]]
            elif current_key is not None:
                current_lines.append(line)
        if current_key:
            result[current_key] = "\n".join(current_lines).strip()
        if result:
            return result

    # 3b. Bare KEY|VALUE lines without START/END markers. Models routinely
    #     emit `DIRECTIONS|[...]` after a prose preamble and forget the
    #     `STRUCTURED_OUTPUT_START/END` wrappers — without this fallback the
    #     entire structured payload is silently lost in the RESPONSE field.
    #     Recognise any line that begins with a SCREAMING_SNAKE key followed
    #     by `|`, and consume continuation lines until the next such key or
    #     end of text.
    bare_keys = list(re.finditer(
        r"^([A-Z][A-Z0-9_-]*)\|(.*)$",
        text,
        re.MULTILINE,
    ))
    if bare_keys:
        result = {}
        for i, m in enumerate(bare_keys):
            key = m.group(1)
            start = m.end(2)  # end of value-on-same-line capture
            end = bare_keys[i + 1].start() if i + 1 < len(bare_keys) else len(text)
            value = (m.group(2) + text[start:end]).strip()
            result[key] = value
        if result:
            return result

    # 4. Find outermost { ... } and try parsing.
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            obj = json.loads(text[first_brace : last_brace + 1])
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass

    return None


_TIER_TO_CLI_MODEL: dict[str, str] = {
    "HAIKU": "haiku",
    "SONNET": "sonnet",
    "OPUS": "opus",
}


@activity.defn(name="spawn_subagent")
async def spawn_subagent(inp: SpawnSubagentInput) -> dict[str, str]:
    # Loud-fail when a workflow forgets to thread `run_dir` through. Every
    # disk-side observability hook in this activity (per-step manifest,
    # cost_audit.jsonl, budget enforcer, claim-check spill, replay cassette,
    # routing/Slack progress) keys off `inp.run_dir` and silently no-ops when
    # it's empty. Dropping a debug-level "skipped" line here was the previous
    # behaviour and produced the failure mode where deep-research ran for
    # months reporting $0.0000 / 0 steps in `sagaflow cost runs` despite
    # spawning hundreds of subagents. Surface at WARNING so the breadcrumb
    # lands in the worker log next to the run that triggered it.
    if not inp.run_dir:
        try:
            _wf_id = activity.info().workflow_id
        except Exception:
            _wf_id = "<unknown>"
        logger.warning(
            "spawn_subagent called without run_dir (workflow=%s role=%s tier=%s); "
            "per-step manifest, cost_audit.jsonl, budget enforcer, and replay "
            "cassette writes are disabled for this call. Pass run_dir on "
            "SpawnSubagentInput from the calling workflow.",
            _wf_id,
            inp.role,
            inp.tier_name,
        )
    prompt_path = Path(inp.user_prompt_path)
    if inp.user_prompt is not None:
        # Inline prompt path — no filesystem read. ``prompt_path`` is still used
        # as a logging label and as the basis for ``label = run_id/role:stem`` below.
        user_prompt = inp.user_prompt
        if not user_prompt.strip():
            raise ValueError(f"inline subagent prompt is empty (label={prompt_path.name})")
    else:
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

    # --- budget pre-dispatch ---
    effective_tier_name = inp.tier_name
    _budget_enforcer = None
    if inp.run_dir:
        from sagaflow.budget.registry import get_enforcer as _get_enforcer
        try:
            _budget_enforcer = _get_enforcer(activity.info().workflow_id)
        except Exception:
            pass
        if _budget_enforcer:
            from sagaflow.budget.enforcer import BudgetExceededError
            resolved_tier, _bstatus = _budget_enforcer.pre_dispatch(inp.role, inp.tier_name)
            if _bstatus.decision.value == "abort":
                raise BudgetExceededError(_bstatus.message)
            effective_tier_name = resolved_tier

    cli = _get_cli()
    run_id = Path(inp.run_dir).name if inp.run_dir else "unknown"
    label = f"{run_id}/{inp.role}:{prompt_path.stem}"

    effective_mcp_config = inp.mcp_config_path
    if inp.tools_needed and not effective_mcp_config:
        _fallback = Path.home() / ".sagaflow" / "mcp-research-minimal.json"
        if _fallback.exists():
            effective_mcp_config = str(_fallback)
            logger.info("No MCP config specified; using fallback minimal config: %s", _fallback)

    if inp.enable_working_memory and inp.run_dir:
        from sagaflow.memory.config import build_mcp_config_with_memory
        effective_mcp_config = str(build_mcp_config_with_memory(
            inp.run_dir,
            agent_role=inp.role,
            base_config_path=effective_mcp_config,
        ))

    # --- CLI dispatch ---
    effective_system = inp.system_prompt
    if inp.enable_working_memory:
        from sagaflow.memory.prompt_fragment import with_working_memory
        effective_system = with_working_memory(effective_system)

    combined_prompt = f"{effective_system}\n\n---\n\n{user_prompt}"
    if inp.output_schema:
        import json as _json
        combined_prompt += (
            "\n\n--- OUTPUT FORMAT ---\n"
            "Respond with a JSON object matching this schema. "
            "Output ONLY the JSON, no markdown fences:\n"
            f"{_json.dumps(inp.output_schema, indent=2)}"
        )
    model_alias = _TIER_TO_CLI_MODEL.get(effective_tier_name, "opus")

    beat_task: asyncio.Task[None] | None = None
    try:
        beat_task = asyncio.create_task(_heartbeat_loop())
    except RuntimeError:
        beat_task = None

    t0 = time.monotonic()
    try:
        cli_result = await cli.call(
            prompt=combined_prompt,
            timeout_seconds=inp.cli_timeout_seconds,
            model=model_alias,
            label=label,
            dangerously_skip_permissions=True,
            mcp_config_path=effective_mcp_config,
        )
    finally:
        if beat_task is not None:
            beat_task.cancel()
            with contextlib.suppress(BaseException):
                await beat_task
    elapsed = round(time.monotonic() - t0, 1)

    raw = cli_result.stdout
    input_tokens = cli_result.input_tokens
    output_tokens = cli_result.output_tokens
    cache_creation_tokens = cli_result.cache_creation_input_tokens
    cache_read_tokens = cli_result.cache_read_input_tokens
    model = model_alias

    # --- budget post-dispatch ---
    if _budget_enforcer:
        from sagaflow.cost import estimate_cost_from_result
        step_cost = estimate_cost_from_result({
            "_input_tokens": str(input_tokens),
            "_output_tokens": str(output_tokens),
            "_cache_creation_tokens": str(cache_creation_tokens),
            "_cache_read_tokens": str(cache_read_tokens),
            "_model": model,
        })
        newly_crossed = _budget_enforcer.record_cost(step_cost)
        if newly_crossed and inp.run_dir:
            from sagaflow.budget.alerts import fire_threshold_alert
            from sagaflow.slack_progress import _read_progress_file
            routing = _read_progress_file(inp.run_dir) or {}
            for threshold in newly_crossed:
                fire_threshold_alert(
                    threshold=threshold,
                    accumulated_cost_usd=_budget_enforcer.ledger.accumulated_cost_usd,
                    max_cost_usd=_budget_enforcer.ledger.policy.max_cost_usd or 0.0,
                    run_id=Path(inp.run_dir).name,
                    slack_channel=routing.get("channel"),
                    slack_thread_ts=routing.get("thread_ts"),
                )

    # --- run manifest recording ---
    if inp.run_dir:
        try:
            from sagaflow.run_manifest import StepRecord, append_step as _append_step
            _append_step(
                Path(inp.run_dir),
                StepRecord(
                    step=inp.step_index,
                    role=inp.role,
                    model=model,
                    tier=effective_tier_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_seconds=elapsed,
                    status="ok",
                    output_schema_used=inp.output_schema is not None,
                ),
            )
        except Exception:
            pass

    _token_meta = {
        "_input_tokens": str(input_tokens),
        "_output_tokens": str(output_tokens),
        "_cache_creation_tokens": str(cache_creation_tokens),
        "_cache_read_tokens": str(cache_read_tokens),
        # Anthropic's authoritative cost from the API response — keep
        # alongside our estimator so callers can compare estimate vs.
        # ground truth and catch rate-card drift early.
        "_total_cost_usd_reported": str(cli_result.total_cost_usd),
        "_model": model,
    }

    # --- automatic cost-accuracy audit ---
    # Append every call's (estimated, reported) pair to a per-run JSONL
    # so drift gets caught without anyone remembering to look. Logs a
    # WARNING when |estimate - reported| / max(reported, 0.001) > 0.20,
    # so chronic drift surfaces in the worker log too.
    if inp.run_dir:
        try:
            from sagaflow.cost import estimate_cost_from_result as _ecfr
            estimated = _ecfr(_token_meta)
            reported = float(cli_result.total_cost_usd or 0.0)
            audit_entry = {
                "ts": time.time(),
                "role": inp.role,
                "tier": effective_tier_name,
                "model": model,
                "estimated_usd": round(estimated, 6),
                "reported_usd": round(reported, 6),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "cache_read_tokens": cache_read_tokens,
                "duration_s": elapsed,
            }
            audit_path = Path(inp.run_dir) / "cost_audit.jsonl"
            with audit_path.open("a", encoding="utf-8") as f:
                import json as _json
                f.write(_json.dumps(audit_entry) + "\n")
            if reported > 0.0:
                drift = (estimated - reported) / reported
                if abs(drift) > 0.20:
                    logger.warning(
                        "cost-estimate drift %s%.0f%% on %s (est=$%.4f reported=$%.4f model=%s)",
                        "+" if drift >= 0 else "",
                        drift * 100,
                        Path(inp.run_dir).name,
                        estimated,
                        reported,
                        model,
                    )
        except Exception:
            logger.debug("cost audit failed", exc_info=True)

    def _record_cassette(output: dict[str, str]) -> None:
        if not inp.run_dir:
            return
        try:
            from sagaflow.replay.cassette import hash_input, record_entry
            record_entry(
                run_dir=Path(inp.run_dir),
                run_id=Path(inp.run_dir).name,
                skill=activity.info().workflow_type or "",
                activity_name="spawn_subagent",
                role=inp.role,
                tier=effective_tier_name,
                input_hash=hash_input(inp.role, inp.system_prompt, user_prompt),
                output=output,
                duration_seconds=elapsed,
            )
        except Exception:
            logger.debug("cassette record failed", exc_info=True)

    # --- response parsing ---
    if inp.output_schema is not None:
        import json
        parsed = _extract_json_object(raw)
        if parsed is not None:
            for k, v in list(parsed.items()):
                if not isinstance(v, str):
                    parsed[k] = json.dumps(v)
            if isinstance(parsed, dict):
                parsed, br = validate_boundary(parsed, label=label)
            if br.truncated_fields:
                parsed["_boundary_truncated"] = ",".join(br.truncated_fields)
                logger.error("TRUNCATED fields in %s: %s", label, br.truncated_fields)
            parsed.update(_token_meta)
            _record_cassette(parsed)
            parsed = spill_large_values(parsed, run_dir=inp.run_dir, activity_label=label)
            return parsed
        logger.warning(
            "Schema-constrained response not valid JSON after extraction attempts "
            "(label=%s, role=%s, raw_len=%d, raw_head=%.200s)",
            label, inp.role, len(raw) if raw else 0, (raw or "")[:200],
        )
        result = {MALFORMED_SENTINEL: "1", "_error": "no valid JSON found", "_raw": raw[:2000], **_token_meta}
        _record_cassette(result)
        return result

    # No output_schema: try structured extraction (handles legacy
    # STRUCTURED_OUTPUT_START/END KEY|VALUE blocks) before falling back to RESPONSE.
    structured = _extract_json_object(raw)
    if isinstance(structured, dict) and structured:
        import json
        for k, v in list(structured.items()):
            if not isinstance(v, str):
                structured[k] = json.dumps(v)
        structured, br = validate_boundary(structured, label=label)
        if br.truncated_fields:
            structured["_boundary_truncated"] = ",".join(br.truncated_fields)
            logger.error("TRUNCATED fields in %s: %s", label, br.truncated_fields)
        structured.update(_token_meta)
        _record_cassette(structured)
        structured = spill_large_values(structured, run_dir=inp.run_dir, activity_label=label)
        return structured

    result = {"RESPONSE": raw or ""}
    result, br = validate_boundary(result, label=label)
    if br.truncated_fields:
        result["_boundary_truncated"] = ",".join(br.truncated_fields)
        logger.error("TRUNCATED fields in %s: %s", label, br.truncated_fields)
    result.update(_token_meta)
    _record_cassette(result)
    result = spill_large_values(result, run_dir=inp.run_dir, activity_label=label)
    return result


@dataclass(frozen=True)
class FinalizeManifestInput:
    run_dir: str
    status: str
    termination_label: str = ""
    error: str = ""


@activity.defn(name="finalize_manifest")
async def finalize_manifest_activity(inp: FinalizeManifestInput) -> None:
    from sagaflow.run_manifest import finalize_manifest

    termination = {"label": inp.termination_label} if inp.termination_label else None
    finalize_manifest(
        run_dir=Path(inp.run_dir),
        status=inp.status,
        termination=termination,
        error=inp.error or None,
    )


@dataclass(frozen=True)
class RunShellInput:
    command: str
    cwd: str = ""
    timeout_seconds: float = 300.0
    env: dict[str, str] | None = None
    label: str = ""


@dataclass(frozen=True)
class RunShellResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


@activity.defn(name="run_shell")
async def run_shell_activity(inp: RunShellInput) -> RunShellResult:
    """Run a shell command deterministically. No agent — just subprocess."""
    activity.heartbeat(f"running: {inp.label or inp.command[:80]}")
    env = {**dict(__import__("os").environ), **(inp.env or {})}
    try:
        proc = await asyncio.create_subprocess_shell(
            inp.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=inp.cwd or None,
            env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=inp.timeout_seconds
        )
        return RunShellResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace")[-10000:],
            stderr=stderr_bytes.decode("utf-8", errors="replace")[-5000:],
            exit_code=proc.returncode or 0,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return RunShellResult(stdout="", stderr="timeout", exit_code=124, timed_out=True)


@dataclass(frozen=True)
class SdkTelemetryInput:
    role: str
    tier: str
    system_prompt: str
    user_prompt: str
    run_dir: str
    step_index: int
    model: str
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    workflow_id: str = ""
    output_schema_used: bool = False


@activity.defn(name="record_sdk_telemetry")
async def record_sdk_telemetry(inp: SdkTelemetryInput) -> str:
    """Record budget, manifest, and cassette for an SDK (Pydantic AI) call."""
    _token_meta = {
        "_input_tokens": str(inp.input_tokens),
        "_output_tokens": str(inp.output_tokens),
        "_model": inp.model,
    }

    if inp.run_dir and inp.workflow_id:
        try:
            from sagaflow.budget.registry import get_enforcer as _get_enforcer
            enforcer = _get_enforcer(inp.workflow_id)
            if enforcer:
                from sagaflow.cost import estimate_cost_from_result
                step_cost = estimate_cost_from_result(_token_meta)
                newly_crossed = enforcer.record_cost(step_cost)
                if newly_crossed:
                    from sagaflow.budget.alerts import fire_threshold_alert
                    from sagaflow.slack_progress import _read_progress_file
                    routing = _read_progress_file(inp.run_dir) or {}
                    for threshold in newly_crossed:
                        fire_threshold_alert(
                            threshold=threshold,
                            accumulated_cost_usd=enforcer.ledger.accumulated_cost_usd,
                            max_cost_usd=enforcer.ledger.policy.max_cost_usd or 0.0,
                            run_id=Path(inp.run_dir).name,
                            slack_channel=routing.get("channel"),
                            slack_thread_ts=routing.get("thread_ts"),
                        )
        except Exception:
            logger.debug("budget recording failed for SDK call", exc_info=True)

    if inp.run_dir:
        try:
            from sagaflow.run_manifest import StepRecord, append_step as _append_step
            _append_step(
                Path(inp.run_dir),
                StepRecord(
                    step=inp.step_index,
                    role=inp.role,
                    model=inp.model,
                    tier=inp.tier,
                    input_tokens=inp.input_tokens,
                    output_tokens=inp.output_tokens,
                    duration_seconds=inp.duration_seconds,
                    status="ok",
                    output_schema_used=inp.output_schema_used,
                ),
            )
        except Exception:
            logger.debug("manifest recording failed for SDK call", exc_info=True)

    if inp.run_dir:
        try:
            from sagaflow.replay.cassette import hash_input, record_entry
            record_entry(
                run_dir=Path(inp.run_dir),
                run_id=Path(inp.run_dir).name,
                skill=inp.workflow_id.split("/")[0] if inp.workflow_id else "",
                activity_name="sdk_agent",
                role=inp.role,
                tier=inp.tier,
                input_hash=hash_input(inp.role, inp.system_prompt, inp.user_prompt),
                output=_token_meta,
                duration_seconds=inp.duration_seconds,
            )
        except Exception:
            logger.debug("cassette record failed for SDK call", exc_info=True)

    return inp.tier


@dataclass(frozen=True)
class BudgetCheckInput:
    workflow_id: str
    role: str
    tier: str


@dataclass(frozen=True)
class BudgetCheckResult:
    effective_tier: str
    abort: bool
    message: str = ""


@activity.defn(name="budget_pre_dispatch")
async def budget_pre_dispatch(inp: BudgetCheckInput) -> BudgetCheckResult:
    """Check budget before an SDK dispatch. Returns effective tier (may downgrade)."""
    try:
        from sagaflow.budget.registry import get_enforcer as _get_enforcer
        enforcer = _get_enforcer(inp.workflow_id)
        if enforcer:
            resolved_tier, status = enforcer.pre_dispatch(inp.role, inp.tier)
            if status.decision.value == "abort":
                return BudgetCheckResult(
                    effective_tier=inp.tier, abort=True, message=status.message,
                )
            return BudgetCheckResult(effective_tier=resolved_tier, abort=False)
    except Exception:
        logger.debug("budget pre-dispatch check failed", exc_info=True)
    return BudgetCheckResult(effective_tier=inp.tier, abort=False)
