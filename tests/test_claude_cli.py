import asyncio
import signal
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


async def test_cancellation_kills_subprocess() -> None:
    """When the calling task is cancelled (e.g. Temporal activity cancel), subprocess is SIGKILL'd.

    Regression test: CancelledError previously fell through without calling _terminate,
    leaving the claude subprocess running indefinitely (observed: 4+ day zombie).
    """
    proc = AsyncMock()
    proc.returncode = None
    proc.pid = 99999

    async def hang(input=None):
        await asyncio.sleep(100)
        return (b"", b"")

    proc.communicate = hang

    killed: list[tuple[int, int]] = []

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("os.getpgid", return_value=99999):
            with patch("os.killpg", side_effect=lambda pgid, sig: killed.append((pgid, sig))):
                transport = ClaudeCliTransport()
                task = asyncio.create_task(transport.call(prompt="p", timeout_seconds=30.0))
                await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert any(sig == signal.SIGKILL for _, sig in killed), (
        "subprocess must be SIGKILL'd when calling task is cancelled; "
        f"got signals: {killed}"
    )


# --- budget cap tests ---

async def test_max_budget_usd_appended_when_set() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b'{"result":"ok","total_cost_usd":0.0,"usage":{}}', b""))
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        transport = ClaudeCliTransport()
        await transport.call(prompt="p", timeout_seconds=30.0, max_budget_usd=8.0)
    args = mock_exec.call_args[0]
    assert "--max-budget-usd" in args, f"--max-budget-usd missing from args: {args}"
    idx = args.index("--max-budget-usd")
    assert args[idx + 1] == "8.0", f"--max-budget-usd value wrong: {args[idx+1]}"


async def test_max_budget_usd_omitted_when_none() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b'{"result":"ok","total_cost_usd":0.0,"usage":{}}', b""))
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        transport = ClaudeCliTransport()
        await transport.call(prompt="p", timeout_seconds=30.0)
    args = mock_exec.call_args[0]
    assert "--max-budget-usd" not in args


async def test_max_budget_usd_omitted_when_zero() -> None:
    """0 disables the cap; should not appear in args."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b'{"result":"ok","total_cost_usd":0.0,"usage":{}}', b""))
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        transport = ClaudeCliTransport()
        await transport.call(prompt="p", timeout_seconds=30.0, max_budget_usd=0.0)
    args = mock_exec.call_args[0]
    assert "--max-budget-usd" not in args


async def test_budget_exhausted_raises_specific_error() -> None:
    """Non-zero exit with budget-related stderr must raise BudgetExhaustedError, not generic."""
    from sagaflow.transport.claude_cli import BudgetExhaustedError
    for stderr_msg in (
        b"max budget exceeded",
        b"budget exhausted",
        b"hit budget limit",
        b"Error: max-budget-usd reached",
        b"BUDGET EXCEEDED for this call",
    ):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", stderr_msg))
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            transport = ClaudeCliTransport()
            with pytest.raises(BudgetExhaustedError) as exc:
                await transport.call(prompt="p", timeout_seconds=30.0, max_budget_usd=8.0)
        assert isinstance(exc.value, ClaudeCliError), "BudgetExhaustedError must subclass ClaudeCliError"


async def test_non_budget_stderr_raises_generic_error() -> None:
    """Stderr without budget keywords must raise generic ClaudeCliError, not BudgetExhaustedError."""
    from sagaflow.transport.claude_cli import BudgetExhaustedError
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b"network timeout"))
    proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        transport = ClaudeCliTransport()
        with pytest.raises(ClaudeCliError) as exc:
            await transport.call(prompt="p", timeout_seconds=30.0)
    # Must be the generic class, not the budget subclass — generic is retryable
    assert not isinstance(exc.value, BudgetExhaustedError), (
        "non-budget failures must raise the retryable generic ClaudeCliError"
    )


async def test_budget_exhausted_in_non_retryable_errors_list() -> None:
    """Belt-and-suspenders: the type name must appear in NON_RETRYABLE_ERRORS so Temporal
    won't retry budget cap-hits 4× and burn 4× the cap."""
    from sagaflow.durable.retry_policies import NON_RETRYABLE_ERRORS
    assert "BudgetExhaustedError" in NON_RETRYABLE_ERRORS
    assert "BudgetExceededError" in NON_RETRYABLE_ERRORS  # workflow-level cap


async def test_split_token_fields_recorded() -> None:
    """v0.10.16: regular input / cache_creation / cache_read recorded separately."""
    import json as _json
    payload = _json.dumps({
        "result": "ok",
        "total_cost_usd": 0.42,
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 500,
            "cache_read_input_tokens": 9400,
            "output_tokens": 200,
        },
    }).encode()
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(payload, b""))
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        transport = ClaudeCliTransport()
        result = await transport.call(prompt="p", timeout_seconds=30.0)
    assert result.input_tokens == 100, "regular input must NOT include cache tokens"
    assert result.cache_creation_input_tokens == 500
    assert result.cache_read_input_tokens == 9400
    assert result.output_tokens == 200
    assert result.total_cost_usd == 0.42


async def test_bare_key_value_extractor() -> None:
    """v0.10.15: model output without STRUCTURED_OUTPUT_START/END markers must still parse."""
    from sagaflow.durable.activities import _extract_json_object
    # Simulates the AIMS round-2 expander output: prose preamble + bare DIRECTIONS|...
    raw = (
        "Looking at round 1 findings, here are follow-ups.\n\n"
        'DIRECTIONS|[{"id":"d_r2_1","question":"q1"},{"id":"d_r2_2","question":"q2"}]\n'
        "FOOTNOTES|other content here"
    )
    result = _extract_json_object(raw)
    assert result is not None, "bare KEY|VALUE must parse without START/END markers"
    assert "DIRECTIONS" in result
    assert "FOOTNOTES" in result
    import json as _json
    parsed = _json.loads(result["DIRECTIONS"])
    assert len(parsed) == 2
    assert parsed[0]["id"] == "d_r2_1"
