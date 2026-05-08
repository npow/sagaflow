"""Claude Code CLI subprocess transport. Use when activities need the Claude Code toolbelt."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ClaudeCliError(RuntimeError):
    """Subprocess failure: nonzero exit, timeout, or transport error."""


@dataclass
class ClaudeCliResult:
    stdout: str
    stderr: str
    exit_code: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0


class ClaudeCliTransport:
    """Spawns `claude -p <prompt>` and returns captured stdout."""

    def __init__(self, command: str = "claude") -> None:
        self._command = command

    async def call(
        self,
        *,
        prompt: str,
        timeout_seconds: float,
        model: str | None = None,
        label: str = "",
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        dangerously_skip_permissions: bool = False,
        mcp_config_path: str | None = None,
    ) -> ClaudeCliResult:
        args = [self._command, "-p", "--output-format", "json"]
        if mcp_config_path:
            args.extend(["--strict-mcp-config", "--mcp-config", mcp_config_path])
        if label:
            args.extend(["--append-system-prompt", f"[sagaflow:{label}]"])
        if model:
            args.extend(["--model", model])
        if dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        elif permission_mode:
            args.extend(["--permission-mode", permission_mode])
        if allowed_tools:
            args.extend(["--allowedTools", *allowed_tools])
        env = os.environ.copy()
        env["IS_SANDBOX"] = "1"
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await _terminate(process)
            raise ClaudeCliError(
                f"`{self._command} -p` timed out after {timeout_seconds}s"
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            if stdout.strip() and "Hook cancelled" in stderr:
                return _parse_json_result(stdout, stderr, process.returncode or 1)
            raise ClaudeCliError(
                f"`{self._command} -p` exited with exit code {process.returncode}: {stderr.strip()}"
            )
        return _parse_json_result(stdout, stderr, process.returncode or 0)


def _parse_json_result(stdout: str, stderr: str, exit_code: int) -> ClaudeCliResult:
    """Extract text result and usage from ``--output-format json`` output."""
    input_tokens = 0
    output_tokens = 0
    total_cost_usd = 0.0
    result_text = stdout

    try:
        data = json.loads(stdout)
        result_text = data.get("result", stdout)
        total_cost_usd = float(data.get("total_cost_usd", 0))
        usage = data.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0)) + int(
            usage.get("cache_creation_input_tokens", 0)
        ) + int(usage.get("cache_read_input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning("Failed to parse JSON from claude CLI output; cost will not be tracked")

    return ClaudeCliResult(
        stdout=result_text,
        stderr=stderr,
        exit_code=exit_code,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd=total_cost_usd,
    )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
