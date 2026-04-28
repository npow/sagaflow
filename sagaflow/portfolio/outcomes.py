"""OutcomeCollector — enrich invocation records with outcome signals."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sagaflow.portfolio.db import default_db_path, get_connection

log = logging.getLogger(__name__)

SIGNAL_WEIGHTS: dict[str, float] = {
    "slack_positive_reaction": 0.3,
    "followup_invoked": 0.5,
    "artifact_present": 0.4,
    "artifact_referenced": 0.8,
    "bv_verdict": 0.7,
    "completed_successfully": 0.6,
}

BV_VERDICT_SCORES: dict[str, float] = {
    "IDENTICAL": 1.0,
    "COSMETIC_CHANGE": 0.9,
    "BEHAVIORAL_CHANGE": 0.5,
    "IMPROVEMENT": 0.8,
    "REGRESSION": 0.2,
    "INCOMPARABLE": 0.0,
}

ARTIFACT_MIN_SIZE_BYTES = 100


@dataclass
class CollectionSummary:
    processed: int
    signals_written: int
    skipped: int
    errors: int


class OutcomeCollector:
    def __init__(
        self,
        db_path: Path | None = None,
        sagaflow_root: Path | None = None,
    ) -> None:
        self._db_path = db_path or default_db_path()
        self._sagaflow_root = sagaflow_root or Path.home() / ".sagaflow"

    def collect(self, since: datetime | None = None) -> CollectionSummary:
        conn = get_connection(self._db_path)
        processed = signals_written = skipped = errors = 0
        try:
            rows = conn.execute(
                "SELECT id, run_id, completion_status, output_artifact_path, slack_message_id "
                "FROM invocations WHERE outcome_collected_at IS NULL"
            ).fetchall()

            for row in rows:
                inv_id = row["id"]
                try:
                    signals = self._collect_signals(row)
                    for sig_type, sig_value, source in signals:
                        conn.execute(
                            "INSERT INTO outcome_signals "
                            "(invocation_id, signal_type, signal_value, source) "
                            "VALUES (?, ?, ?, ?)",
                            (inv_id, sig_type, sig_value, source),
                        )
                        signals_written += 1
                    conn.execute(
                        "UPDATE invocations SET outcome_collected_at = datetime('now') "
                        "WHERE id = ?",
                        (inv_id,),
                    )
                    conn.commit()
                    processed += 1
                except Exception:
                    log.exception("Outcome collection failed for invocation %d", inv_id)
                    errors += 1
        finally:
            conn.close()

        return CollectionSummary(
            processed=processed,
            signals_written=signals_written,
            skipped=skipped,
            errors=errors,
        )

    def _collect_signals(self, row) -> list[tuple[str, float, str]]:
        signals: list[tuple[str, float, str]] = []

        if row["completion_status"] == "success":
            signals.append(("completed_successfully", 1.0, "run_status"))
        else:
            signals.append(("completed_successfully", 0.0, "run_status"))

        artifact_path = row["output_artifact_path"]
        if artifact_path:
            full_path = self._sagaflow_root / artifact_path
            if full_path.exists() and full_path.stat().st_size > ARTIFACT_MIN_SIZE_BYTES:
                signals.append(("artifact_present", 1.0, "filesystem"))
            else:
                signals.append(("artifact_present", 0.0, "filesystem"))

        run_id = row["run_id"]
        verdict = self._lookup_bv_verdict(run_id)
        if verdict is not None:
            score = BV_VERDICT_SCORES.get(verdict, 0.5)
            signals.append(("bv_verdict", score, "behavior_versioning"))

        return signals

    def _lookup_bv_verdict(self, run_id: str) -> str | None:
        manifest_path = self._sagaflow_root / "runs" / run_id / "run_manifest.json"
        if not manifest_path.exists():
            return None
        try:
            data = json.loads(manifest_path.read_text())
            return data.get("verdict")
        except Exception:
            return None
