"""Activity-output claim-check helper.

Temporal serializes every activity return value into workflow history, with a
~2MB hard cap per event payload (TMPRL1103). Activities that return text-heavy
dicts — full LLM responses, bash stdout, file contents — are bombs the moment
their output crosses the cap.

`spill_large_values` walks an activity-return dict and replaces any value
above `threshold_bytes` with a claim-check pointer:

    {"_claim_check_path": "...", "_size_bytes": N, "_truncated": False}

The full content is written to disk under `{run_dir}/.claim-check/{stem}-{key}.txt`.
Callers (workflows or downstream activities) that need the full content read it
from disk via the path; small fields pass through unchanged.

This is the ONE pattern every sagaflow activity returning user-text should use
at the return site. It keeps payloads in workflow history bounded regardless of
what an LLM, subprocess, or file read produces.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_BYTES = 65_536
CLAIM_CHECK_DIR = ".claim-check"


def _value_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    return 0


def spill_large_values(
    payload: dict[str, Any],
    *,
    run_dir: str | Path | None,
    activity_label: str,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
) -> dict[str, Any]:
    """Spill any payload value whose UTF-8 size exceeds `threshold_bytes` to
    disk and replace it with a claim-check pointer.

    Args:
        payload: The dict the activity is about to return. Mutated in place
            and also returned for chaining.
        run_dir: The run directory. If falsy, no spill is performed (the
            payload is returned unchanged) — the caller is responsible for
            downstream size enforcement.
        activity_label: Used to namespace claim-check files and as a logging
            tag. Should be unique per spawn (e.g. `{role}/{prompt_stem}`).
        threshold_bytes: Per-value byte cap. Defaults to 64KB; set lower for
            activities expected to return tiny dicts.

    Returns:
        The same dict, with large values replaced by claim-check stubs.
    """
    if not run_dir:
        return payload
    run_path = Path(run_dir)
    if not run_path.is_dir():
        return payload
    base = run_path / CLAIM_CHECK_DIR
    safe_label = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in activity_label)
    spilled: list[str] = []
    for key, value in list(payload.items()):
        size = _value_size(value)
        if size <= threshold_bytes:
            continue
        try:
            base.mkdir(parents=True, exist_ok=True)
            target = base / f"{safe_label}-{key}.txt"
            if isinstance(value, (bytes, bytearray)):
                target.write_bytes(bytes(value))
            else:
                target.write_text(str(value), encoding="utf-8")
            payload[key] = {
                "_claim_check_path": str(target),
                "_size_bytes": size,
                "_truncated": False,
            }
            spilled.append(f"{key}={size}B")
        except OSError as exc:
            # Failure to write to disk is rare but recoverable: truncate the
            # value rather than blow past the payload cap silently. Loud over
            # silent — the caller sees the marker.
            payload[key] = {
                "_claim_check_path": None,
                "_size_bytes": size,
                "_truncated": True,
                "_truncation_reason": str(exc),
                "_excerpt": (str(value)[:2048] if isinstance(value, str) else ""),
            }
            spilled.append(f"{key}=TRUNCATED({size}B,{exc})")
    if spilled:
        logger.info(
            "claim-check spill: activity=%s entries=%s",
            activity_label,
            ", ".join(spilled),
        )
    return payload
