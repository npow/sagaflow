"""Claude Code CLI subprocess transport. Use when activities need the Claude Code toolbelt."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class ClaudeCliError(RuntimeError):
    """Subprocess failure: nonzero exit, timeout, or transport error."""


@dataclass
class ClaudeCliResult:
    stdout: str
    stderr: str
    exit_code: int


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
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        dangerously_skip_permissions: bool = False,
    ) -> ClaudeCliResult:
        args = [self._command, "-p", prompt]
        if model:
            args.extend(["--model", model])
        if dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        elif permission_mode:
            args.extend(["--permission-mode", permission_mode])
        if allowed_tools:
            args.extend(["--allowedTools", *allowed_tools])
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            await _terminate(process)
            raise ClaudeCliError(
                f"`{self._command} -p` timed out after {timeout_seconds}s"
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ClaudeCliError(
                f"`{self._command} -p` exited with exit code {process.returncode}: {stderr.strip()}"
            )
        return ClaudeCliResult(stdout=stdout, stderr=stderr, exit_code=process.returncode)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass
