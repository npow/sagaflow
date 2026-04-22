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
    for line in body.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        key, _, value = line.partition("|")
        result[key.strip()] = value.strip()

    if not result:
        raise MalformedResponseError(
            "Structured block has no parseable KEY|VALUE lines"
        )

    return result
