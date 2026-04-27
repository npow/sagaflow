"""Parses STRUCTURED_OUTPUT_START/END blocks from subagent responses.

Contract: subagents emit machine-parseable `KEY|VALUE` lines between markers.
Anything outside the block is ignored. When multiple blocks are present the
LAST one wins (allows subagents to revise their answer).

Fallback: when markers are absent, scans the full text for KEY|VALUE lines.
This handles the common case where the LLM returns valid structured data
but omits the wrapper markers.

Missing or empty block → MalformedResponseError. Callers use the shared
execution-model-contracts fail-safe rule (return the WORST legal value).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

START_MARKER = "STRUCTURED_OUTPUT_START"
END_MARKER = "STRUCTURED_OUTPUT_END"

_BLOCK_PATTERN = re.compile(
    rf"{re.escape(START_MARKER)}\s*(?P<body>.*?)\s*{re.escape(END_MARKER)}",
    re.DOTALL,
)

_KV_LINE = re.compile(r"[A-Z][A-Z0-9_]*")


class MalformedResponseError(ValueError):
    """Raised when the subagent response lacks a parseable structured block."""


def _parse_kv_lines(text: str) -> dict[str, str]:
    """Extract KEY|VALUE pairs from text. Returns empty dict if none found."""
    result: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if current_key is not None:
                result[current_key] += "\n"
            continue
        if "|" in line:
            key, _, value = line.partition("|")
            key = key.strip()
            if _KV_LINE.fullmatch(key):
                result[key] = value.strip()
                current_key = key
                continue
        if current_key is not None:
            result[current_key] += "\n" + line
    return result


def parse_structured(text: str) -> dict[str, str]:
    """Return the key-value pairs from the LAST structured block in ``text``.

    Tries marker-delimited blocks first. Falls back to scanning the full
    text for KEY|VALUE lines when markers are absent.

    Raises MalformedResponseError if no parseable content is found.
    """

    matches = list(_BLOCK_PATTERN.finditer(text))
    if matches:
        body = matches[-1].group("body").strip()
        if not body:
            raise MalformedResponseError("Structured block is present but empty")
        result = _parse_kv_lines(body)
        if result:
            return result
        raise MalformedResponseError(
            "Structured block has no parseable KEY|VALUE lines"
        )

    # Fallback: no markers. Scan raw text for KEY|VALUE lines.
    result = _parse_kv_lines(text)
    if result:
        logger.info(
            "No %s/%s markers but found %d KEY|VALUE pair(s) via fallback",
            START_MARKER, END_MARKER, len(result),
        )
        return result

    raise MalformedResponseError(
        f"Response contains no {START_MARKER}/{END_MARKER} block "
        "and no KEY|VALUE lines found in raw text"
    )
