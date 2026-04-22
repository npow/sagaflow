import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skillflow.transport.claude_cli import ClaudeCliTransport, ClaudeCliResult, ClaudeCliError


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
    async def never_communicates():
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
