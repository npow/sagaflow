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

    async def call(self, *, prompt: str, timeout_seconds: float) -> ClaudeCliResult:
        process = await asyncio.create_subprocess_exec(
            self._command,
            "-p",
            prompt,
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
