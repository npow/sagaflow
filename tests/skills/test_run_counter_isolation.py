"""Regression: per-test counter factory prevents shared-state pollution.

Before the fix, a module-level _run_counter leaked across tests,
producing `assert 1 == 2` when a prior test left stale entries.
"""

from __future__ import annotations

from tests.skills.test_flaky_test_diagnoser import _make_fake_run_test_subprocess


async def test_counter_isolation_across_batches() -> None:
    """Two independent factory calls get independent counters."""
    fake_run_1, counter_1 = _make_fake_run_test_subprocess()
    fake_run_2, counter_2 = _make_fake_run_test_subprocess()

    await fake_run_1("batch1-cmd")
    assert len(counter_1) == 1
    assert len(counter_2) == 0  # unaffected

    await fake_run_2("batch2-cmd")
    assert len(counter_2) == 1  # would have been 2 with shared state
    assert len(counter_1) == 1  # still unaffected
