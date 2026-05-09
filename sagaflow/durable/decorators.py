"""Sagaflow's wrapper around `@activity.defn` that enforces the claim-check
invariant on every registered activity.

Use `@sagaflow_activity()` in place of `@activity.defn(name=...)` for any new
activity. The wrapper:

1. Registers the function with Temporal exactly like `@activity.defn` would.
2. Wraps the implementation so that whenever the return value is a `dict`,
   every value larger than `threshold_bytes` (default 64 KB) is spilled to
   disk via `spill_large_values` and replaced with a claim-check pointer
   `{_claim_check_path, _size_bytes}`.
3. Reads `run_dir` from the activity's input dataclass (first positional
   argument's `.run_dir` attribute, or the `run_dir` keyword argument).
   If neither is available, the wrapper logs a warning and returns the
   un-spilled result — large payloads will then be caught by the runtime's
   non-retryable `PayloadTooLargeError` policy and fail loudly rather
   than silently retry.

The point: skill authors who use `@sagaflow_activity` cannot accidentally
return a 2 MB blob through a Temporal event payload. The runtime enforces
the invariant; nobody has to remember to call `spill_large_values` by hand.

For activities that genuinely don't have a `run_dir` (e.g. utility helpers
that produce only small returns), pass `auto_spill=False` to opt out
explicitly. Opting out is auditable in code review.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

from temporalio import activity

from sagaflow.durable.claim_check import DEFAULT_THRESHOLD_BYTES, spill_large_values

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _extract_run_dir(args: tuple, kwargs: dict) -> str | None:
    """Best-effort lookup of `run_dir` from activity args."""
    if args:
        first = args[0]
        run_dir = getattr(first, "run_dir", None)
        if run_dir:
            return str(run_dir)
    rd = kwargs.get("run_dir")
    if rd:
        return str(rd)
    return None


def sagaflow_activity(
    name: str | None = None,
    *,
    auto_spill: bool = True,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
) -> Callable[[F], F]:
    """Stand-in for `@activity.defn` that auto-applies the claim-check.

    Usage:
        @sagaflow_activity(name="my_activity")
        async def my_activity(inp: MyInput) -> dict:
            ...
            return {"some_field": large_text}

    The returned dict will have any value > `threshold_bytes` spilled to
    `{inp.run_dir}/.claim-check/{name}-{key}.txt` automatically. Workflow
    payload stays tiny.
    """

    def decorator(func: F) -> F:
        activity_name = name or func.__name__

        if not auto_spill:
            return activity.defn(name=activity_name)(func)  # type: ignore[return-value]

        @functools.wraps(func)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            if not isinstance(result, dict):
                return result
            run_dir = _extract_run_dir(args, kwargs)
            if not run_dir:
                # Soft fallback: no run_dir, no spill. Large returns will
                # surface as PayloadTooLargeError (now non-retryable) rather
                # than silently retrying for hours. Logged so the missing
                # run_dir is visible in operator triage.
                size_total = sum(
                    len(v.encode("utf-8")) for v in result.values() if isinstance(v, str)
                )
                if size_total > threshold_bytes:
                    logger.warning(
                        "sagaflow_activity %s returned %dB but no run_dir found "
                        "in args; claim-check skipped. Large payload may trigger "
                        "PayloadTooLargeError on the next workflow event.",
                        activity_name,
                        size_total,
                    )
                return result
            return spill_large_values(
                result,
                run_dir=run_dir,
                activity_label=activity_name,
                threshold_bytes=threshold_bytes,
            )

        return activity.defn(name=activity_name)(wrapped)  # type: ignore[return-value]

    return decorator
