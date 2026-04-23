"""Parses STRUCTURED_OUTPUT_START/END blocks from subagent responses.

Contract: subagents emit machine-parseable `KEY|VALUE` lines between markers.
Anything outside the block is ignored. When multiple blocks are present the
LAST one wins (allows subagents to revise their answer).

Missing or empty block → MalformedResponseError. Callers use the shared
execution-model-contracts fail-safe rule (return the WORST legal value).
"""

from __future__ import annotations

import re

START_MARKER = "STRUCTURED_OUTPUT_START"
END_MARKER = "STRUCTURED_OUTPUT_END"

_BLOCK_PATTERN = re.compile(
    rf"{re.escape(START_MARKER)}\s*(?P<body>.*?)\s*{re.escape(END_MARKER)}",
    re.DOTALL,
)


class MalformedResponseError(ValueError):
    """Raised when the subagent response lacks a parseable structured block."""


def parse_structured(text: str) -> dict[str, str]:
    """Return the key-value pairs from the LAST structured block in ``text``.

    Raises MalformedResponseError if no complete block exists or the block is empty.
    """

    matches = list(_BLOCK_PATTERN.finditer(text))
    if not matches:
        raise MalformedResponseError(
            f"Response contains no {START_MARKER}/{END_MARKER} block"
        )

    body = matches[-1].group("body").strip()
    if not body:
        raise MalformedResponseError("Structured block is present but empty")

    result: dict[str, str] = {}
    current_key: str | None = None
    for line in body.splitlines():
        line = line.strip()
        if not line:
            # Blank lines inside a multi-line value are preserved.
            if current_key is not None:
                result[current_key] += "\n"
            continue
        if "|" in line:
            key, _, value = line.partition("|")
            key = key.strip()
            # Heuristic: known keys are ALL_CAPS (possibly with digits/underscores).
            # Lines where the part before the first pipe doesn't look like a key
            # are continuation lines of the previous value.
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                result[key] = value.strip()
                current_key = key
                continue
        # Continuation line — append to previous key's value.
        if current_key is not None:
            result[current_key] += "\n" + line
        # else: stray line before first key — ignore

    if not result:
        raise MalformedResponseError(
            "Structured block has no parseable KEY|VALUE lines"
        )

    return result
