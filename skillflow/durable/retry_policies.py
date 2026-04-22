"""Shared retry policies for every skillflow activity.

Tiers: Haiku (cheap, fast, more retries OK), Sonnet (dearer, identical policy
currently), CLI subprocess (longer intervals because cold-start is expensive)."""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

NON_RETRYABLE_ERRORS: list[str] = [
    "InvalidInputError",
    "MalformedResponseError",
]


HAIKU_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=2,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)

SONNET_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=2,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)

CLI_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=2,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)
