"""Shared retry policies for every sagaflow activity.

Tiers: Haiku (cheap, fast, more retries OK), Sonnet (dearer, identical policy
currently), CLI subprocess (longer intervals because cold-start is expensive)."""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

NON_RETRYABLE_ERRORS: list[str] = [
    "InvalidInputError",
    "MalformedResponseError",
    "AbortRequestedError",
    # Temporal payload-size violations are deterministic — retrying never
    # helps. The Temporal SDK raises these as the activity input/output
    # crosses the 2MB grpc cap (TMPRL1103). Without this entry, the worker
    # retries up to maximum_attempts × cli_timeout_seconds (4 × 1h = 4h)
    # before giving up — which is the failure mode that left the
    # deep-research run silently wedged for hours.
    "PayloadTooLargeError",
    "WorkflowPayloadSizeError",
    "PayloadSizeError",
]


HAIKU_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=4,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)

SONNET_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=4,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)

CLI_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=15),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=120),
    maximum_attempts=4,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)
