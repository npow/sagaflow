"""System-boundary validation for data entering sagaflow from external sources.

Validates MCP tool responses and subagent outputs at the boundary before
they propagate into agent reasoning.  Fail fast with clear errors instead
of letting malformed external data silently corrupt downstream agents.

Two entry points:

* ``validate_boundary(data)``  — dict responses from ``spawn_subagent``
* ``validate_text_boundary(text)`` — raw text written via ``write_artifact``
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_FIELD_BYTES = 50_000
MAX_RESPONSE_BYTES = 500_000
MAX_FIELDS = 100

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"<\s*system\s*>", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all|any|the)\s+", re.IGNORECASE),
    re.compile(r"override\s+(?:your|all|the)\s+(?:instructions|rules|prompt)", re.IGNORECASE),
    re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
]

_INTERNAL_KEYS = frozenset({
    "_raw", "_error", "_raw_path", "_sagaflow_malformed",
    "_boundary_injection_flags", "_boundary_truncated",
})


@dataclass
class BoundaryViolation:
    field: str
    severity: str  # "warning" | "error"
    message: str


@dataclass
class BoundaryResult:
    violations: list[BoundaryViolation] = field(default_factory=list)
    truncated_fields: list[str] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)

    @property
    def has_warnings(self) -> bool:
        return bool(self.violations) or bool(self.injection_flags)


def validate_boundary(
    data: dict[str, str],
    *,
    max_field_bytes: int = MAX_FIELD_BYTES,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    max_fields: int = MAX_FIELDS,
    check_injection: bool = True,
    required_fields: frozenset[str] | None = None,
    label: str = "",
) -> tuple[dict[str, str], BoundaryResult]:
    """Validate and sanitize a parsed response dict at a system boundary.

    Returns ``(sanitized_data, result)``.  Oversized fields are truncated;
    injection patterns are flagged (log-only, never blocked).
    """
    result = BoundaryResult()
    sanitized: dict[str, str] = {}

    if len(data) > max_fields:
        result.violations.append(BoundaryViolation(
            field="_count",
            severity="error",
            message=f"Response has {len(data)} fields (max {max_fields})",
        ))
        items = list(data.items())[:max_fields]
    else:
        items = list(data.items())

    total_bytes = 0
    for key, value in items:
        if not isinstance(value, str):
            value = str(value)

        value_bytes = len(value.encode("utf-8", errors="replace"))
        total_bytes += value_bytes

        if value_bytes > max_field_bytes:
            result.violations.append(BoundaryViolation(
                field=key,
                severity="warning",
                message=f"Field truncated: {value_bytes} bytes > {max_field_bytes} limit",
            ))
            result.truncated_fields.append(key)
            value = value[:max_field_bytes] + "\n[TRUNCATED by boundary validation]"

        if check_injection and key not in _INTERNAL_KEYS:
            scan_window = value[:2000]
            for pattern in _INJECTION_PATTERNS:
                match = pattern.search(scan_window)
                if match:
                    result.injection_flags.append(
                        f"{key}: pattern '{match.group()}' at pos {match.start()}"
                    )
                    break

        sanitized[key] = value

    if total_bytes > max_response_bytes:
        result.violations.append(BoundaryViolation(
            field="_total",
            severity="warning",
            message=f"Total response {total_bytes} bytes > {max_response_bytes} limit",
        ))

    if required_fields:
        for f in sorted(required_fields - frozenset(sanitized.keys())):
            result.violations.append(BoundaryViolation(
                field=f,
                severity="error",
                message=f"Required field '{f}' missing",
            ))

    if result.has_warnings:
        logger.warning(
            "Boundary [%s]: %d violations, %d truncated, %d injection flags",
            label or "unlabeled",
            len(result.violations),
            len(result.truncated_fields),
            len(result.injection_flags),
        )
        if result.injection_flags:
            logger.warning(
                "Injection flags [%s]: %s", label, "; ".join(result.injection_flags)
            )

    return sanitized, result


def validate_text_boundary(
    text: str,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    check_injection: bool = True,
    label: str = "",
) -> tuple[str, BoundaryResult]:
    """Validate raw text at a system boundary.

    Intended for ``write_artifact`` content — data written to files that
    downstream agents will consume.  Truncates oversized text and flags
    injection patterns (log-only).
    """
    result = BoundaryResult()

    text_bytes = len(text.encode("utf-8", errors="replace"))
    if text_bytes > max_bytes:
        result.violations.append(BoundaryViolation(
            field="_text",
            severity="warning",
            message=f"Text truncated: {text_bytes} bytes > {max_bytes} limit",
        ))
        text = text[:max_bytes] + "\n[TRUNCATED by boundary validation]"

    if check_injection:
        scan_window = text[:5000]
        for pattern in _INJECTION_PATTERNS:
            match = pattern.search(scan_window)
            if match:
                result.injection_flags.append(
                    f"pattern '{match.group()}' at pos {match.start()}"
                )

    if result.has_warnings:
        logger.warning(
            "Text boundary [%s]: %d violations, %d injection flags",
            label or "unlabeled",
            len(result.violations),
            len(result.injection_flags),
        )

    return text, result
