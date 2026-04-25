import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sagaflow.transport.claude_cli import ClaudeCliTransport, ClaudeCliResult, ClaudeCliError


@pytest.fixture
def fake_process():
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"hello from subprocess\n", b""))
    proc.returncode = 0
    return proc


async def test_call_captures_stdout(fake_process) -> None:
    with patch("asyncio.create_subprocess_exec", return_value=fake_process):
        transport = ClaudeCliTransport()
        result = await transport.call(prompt="say hi", timeout_seconds=30.0)
    assert isinstance(result, ClaudeCliResult)
    assert result.stdout == "hello from subprocess\n"
    assert result.exit_code == 0


async def test_prompt_passed_via_stdin_not_args(fake_process) -> None:
    """Prompt must go through stdin, not command-line args (ARG_MAX limit)."""
    with patch("asyncio.create_subprocess_exec", return_value=fake_process) as mock_exec:
        transport = ClaudeCliTransport()
        big_prompt = "x" * 500_000
        await transport.call(prompt=big_prompt, timeout_seconds=30.0)

    # Args must NOT contain the prompt
    called_args = mock_exec.call_args[0]
    for arg in called_args:
        assert len(str(arg)) < 1000, f"Prompt leaked into command args (len={len(str(arg))})"

    # Prompt must be piped via stdin
    assert mock_exec.call_args[1].get("stdin") is not None, "stdin not set on subprocess"
    fake_process.communicate.assert_called_once()
    call_kwargs = fake_process.communicate.call_args
    stdin_input = call_kwargs[1].get("input") or (call_kwargs[0][0] if call_kwargs[0] else None)
    assert stdin_input is not None, "Prompt not passed via communicate(input=)"
    assert len(stdin_input) == 500_000, "Stdin input doesn't match prompt size"


async def test_hook_cancelled_returns_result_not_error() -> None:
    """When a hook cancels but stdout has content, return result instead of raising."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"useful output", b"Hook cancelled"))
    proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        transport = ClaudeCliTransport()
        result = await transport.call(prompt="p", timeout_seconds=30.0)
    assert result.stdout == "useful output"
    assert result.exit_code == 1


async def test_call_raises_on_nonzero_exit() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b"boom"))
    proc.returncode = 7
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        transport = ClaudeCliTransport()
        with pytest.raises(ClaudeCliError) as exc:
            await transport.call(prompt="p", timeout_seconds=30.0)
    assert "exit code 7" in str(exc.value)
    assert "boom" in str(exc.value)


async def test_call_raises_on_timeout() -> None:
    async def never_communicates(input=None):
        await asyncio.sleep(10.0)
        return (b"", b"")

    proc = AsyncMock()
    proc.communicate = never_communicates
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        transport = ClaudeCliTransport()
        with pytest.raises(ClaudeCliError) as exc:
            await transport.call(prompt="p", timeout_seconds=0.1)
    assert "timed out" in str(exc.value).lower()
    proc.kill.assert_called()
