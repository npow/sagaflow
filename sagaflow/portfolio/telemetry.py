"""TelemetryWriter — non-blocking write-behind telemetry for skill invocations."""

from __future__ import annotations

import atexit
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sagaflow.portfolio.db import db_exists, default_db_path

log = logging.getLogger(__name__)

MAX_QUEUE_DEPTH = 10_000
FLUSH_TIMEOUT_SECONDS = 2.0

_INSERT_SQL = """\
INSERT OR IGNORE INTO invocations (
    run_id, skill_name, trigger_context, invoker_type,
    started_at, completed_at, completion_status,
    input_token_count, output_token_count, model_name,
    input_size_bytes, output_artifact_path, slack_message_id, source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass
class InvocationRecord:
    run_id: str
    skill_name: str
    trigger_context: str = "unknown"
    invoker_type: str = "unknown"
    started_at: str = ""
    completed_at: str = ""
    completion_status: str = "unknown"
    input_token_count: int | None = None
    output_token_count: int | None = None
    model_name: str | None = None
    input_size_bytes: int | None = None
    output_artifact_path: str | None = None
    slack_message_id: str | None = None
    source: str = "live"


class TelemetryWriter:
    """Non-blocking telemetry writer backed by a bounded queue and a background thread."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_db_path()
        self._queue: queue.Queue[InvocationRecord | None] = queue.Queue(
            maxsize=MAX_QUEUE_DEPTH
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._write_loop, daemon=True, name="pef-writer"
        )
        self._thread.start()
        atexit.register(self.flush)

    def enqueue(self, record: InvocationRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            log.warning(
                "Portfolio telemetry queue full (%d); dropping record run_id=%s",
                MAX_QUEUE_DEPTH,
                record.run_id,
            )

    def flush(self, timeout: float = FLUSH_TIMEOUT_SECONDS) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)
        remaining = self._queue.qsize()
        if remaining:
            log.warning("Portfolio telemetry flush timeout; %d records dropped", remaining)

    def _write_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain()
            except Exception:
                log.exception("Portfolio telemetry write error; restarting in 1s")
                self._stop.wait(1.0)
        try:
            self._drain()
        except Exception:
            log.exception("Portfolio telemetry final drain failed")

    def _drain(self) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    record = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if record is None:
                    break
                conn.execute(
                    _INSERT_SQL,
                    (
                        record.run_id,
                        record.skill_name,
                        record.trigger_context,
                        record.invoker_type,
                        record.started_at,
                        record.completed_at,
                        record.completion_status,
                        record.input_token_count,
                        record.output_token_count,
                        record.model_name,
                        record.input_size_bytes,
                        record.output_artifact_path,
                        record.slack_message_id,
                        record.source,
                    ),
                )
                conn.commit()
        finally:
            conn.close()


class NullTelemetryWriter:
    """Drop-in replacement that silently discards all records."""

    def enqueue(self, record: InvocationRecord) -> None:
        pass

    def flush(self, timeout: float = FLUSH_TIMEOUT_SECONDS) -> None:
        pass


_writer_instance: TelemetryWriter | NullTelemetryWriter | None = None
_writer_lock = threading.Lock()


def get_writer(db_path: Path | None = None) -> TelemetryWriter | NullTelemetryWriter:
    global _writer_instance
    if _writer_instance is not None:
        return _writer_instance
    with _writer_lock:
        if _writer_instance is not None:
            return _writer_instance
        path = db_path or default_db_path()
        if db_exists(path):
            _writer_instance = TelemetryWriter(path)
        else:
            _writer_instance = NullTelemetryWriter()
        return _writer_instance


def reset_writer() -> None:
    """Reset the singleton (for testing)."""
    global _writer_instance
    with _writer_lock:
        if _writer_instance is not None and isinstance(_writer_instance, TelemetryWriter):
            _writer_instance.flush()
        _writer_instance = None
