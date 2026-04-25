from datetime import timedelta

from sagaflow.durable.retry_policies import (
    CLI_POLICY,
    HAIKU_POLICY,
    NON_RETRYABLE_ERRORS,
    SONNET_POLICY,
)


def test_policies_have_expected_attempts() -> None:
    assert HAIKU_POLICY.maximum_attempts == 4
    assert SONNET_POLICY.maximum_attempts == 4
    assert CLI_POLICY.maximum_attempts == 4


def test_haiku_policy_intervals() -> None:
    assert HAIKU_POLICY.initial_interval == timedelta(seconds=10)
    assert HAIKU_POLICY.backoff_coefficient == 2.0
    assert HAIKU_POLICY.maximum_interval == timedelta(seconds=60)


def test_sonnet_policy_intervals() -> None:
    assert SONNET_POLICY.initial_interval == timedelta(seconds=10)
    assert SONNET_POLICY.backoff_coefficient == 2.0
    assert SONNET_POLICY.maximum_interval == timedelta(seconds=60)


def test_cli_policy_has_longer_intervals() -> None:
    assert CLI_POLICY.initial_interval == timedelta(seconds=15)
    assert CLI_POLICY.maximum_interval == timedelta(seconds=120)


def test_non_retryable_list_is_explicit() -> None:
    assert "InvalidInputError" in NON_RETRYABLE_ERRORS
    assert "MalformedResponseError" in NON_RETRYABLE_ERRORS


def test_policies_share_non_retryable_list() -> None:
    assert HAIKU_POLICY.non_retryable_error_types == NON_RETRYABLE_ERRORS
    assert SONNET_POLICY.non_retryable_error_types == NON_RETRYABLE_ERRORS
    assert CLI_POLICY.non_retryable_error_types == NON_RETRYABLE_ERRORS
