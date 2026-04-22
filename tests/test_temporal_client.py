import pytest

from sagaflow.temporal_client import (
    DEFAULT_NAMESPACE,
    DEFAULT_TARGET,
    TASK_QUEUE,
    TemporalUnreachable,
    preflight,
)


async def test_preflight_fails_fast_on_bad_target() -> None:
    with pytest.raises(TemporalUnreachable) as exc:
        await preflight(target="127.0.0.1:1", timeout_seconds=0.2)
    assert "127.0.0.1:1" in str(exc.value)


async def test_preflight_succeeds_against_running_server() -> None:
    # Skipped in CI; locally verifies the real dev server.
    pytest.importorskip("temporalio")
    try:
        await preflight(target=DEFAULT_TARGET, timeout_seconds=2.0)
    except TemporalUnreachable:
        pytest.skip("local Temporal dev server not running")


def test_constants_hold_expected_defaults() -> None:
    assert DEFAULT_TARGET == "localhost:7233"
    assert DEFAULT_NAMESPACE == "default"
    assert TASK_QUEUE == "sagaflow"
