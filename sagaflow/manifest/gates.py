"""Gate evaluators for manifest step validation."""

from __future__ import annotations

from typing import Any

from sagaflow.manifest.schema import Gate
from sagaflow.manifest.context import ExecutionContext


class GateError(Exception):
    pass


class GateEvaluator:

    def evaluate(self, gate: Gate, ctx: ExecutionContext) -> bool:
        value = ctx.get(gate.field)
        match gate.type:
            case "non_empty":
                return bool(value)
            case "falsifiability":
                return self._check_falsifiability(value, gate.min_hypotheses or 1)
            case "rubber_stamp":
                ref = ctx.get(gate.reference_field) if gate.reference_field else None
                return self._check_rubber_stamp(value, ref, gate.similarity_threshold or 0.85)
            case "field_truthy":
                return bool(value)
            case "min_length":
                return isinstance(value, (list, str)) and len(value) >= (gate.min_hypotheses or 1)
            case "mode_match":
                return str(value) == str(gate.value)
            case "custom":
                raise GateError(
                    f"Custom gate '{gate.custom_activity}' requires activity dispatch — "
                    f"not supported in synchronous evaluation"
                )
            case _:
                raise GateError(f"Unknown gate type: {gate.type!r}")

    def _check_falsifiability(self, hypotheses: Any, min_count: int) -> bool:
        if not isinstance(hypotheses, list):
            return False
        falsifiable = [
            h for h in hypotheses
            if isinstance(h, dict) and h.get("test") and h.get("expected_outcome")
        ]
        return len(falsifiable) >= min_count

    def _check_rubber_stamp(self, new: Any, reference: Any, threshold: float) -> bool:
        """Gate PASSES when overlap < threshold (sufficiently novel)."""
        if not new or not reference:
            return True
        new_tokens = set(str(new).lower().split())
        ref_tokens = set(str(reference).lower().split())
        if not ref_tokens:
            return True
        overlap = len(new_tokens & ref_tokens) / len(ref_tokens)
        return overlap < threshold
