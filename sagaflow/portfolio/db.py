"""Portfolio database initialization, schema migration, and connection management."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS invocations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL UNIQUE,
    skill_name            TEXT    NOT NULL,
    trigger_context       TEXT    NOT NULL DEFAULT 'unknown',
    invoker_type          TEXT    NOT NULL DEFAULT 'unknown',
    started_at            TEXT    NOT NULL,
    completed_at          TEXT    NOT NULL,
    completion_status     TEXT    NOT NULL,
    input_token_count     INTEGER,
    output_token_count    INTEGER,
    model_name            TEXT,
    input_size_bytes      INTEGER,
    output_artifact_path  TEXT,
    slack_message_id      TEXT,
    source                TEXT    NOT NULL DEFAULT 'live',
    outcome_collected_at  TEXT,
    exported_at           TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_invocations_skill_name        ON invocations(skill_name);
CREATE INDEX IF NOT EXISTS idx_invocations_started_at        ON invocations(started_at);
CREATE INDEX IF NOT EXISTS idx_invocations_outcome_collected ON invocations(outcome_collected_at);
CREATE INDEX IF NOT EXISTS idx_invocations_source            ON invocations(source);

CREATE TABLE IF NOT EXISTS outcome_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    invocation_id  INTEGER NOT NULL REFERENCES invocations(id),
    signal_type    TEXT    NOT NULL,
    signal_value   REAL    NOT NULL,
    collected_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    source         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcome_signals_invocation ON outcome_signals(invocation_id);

CREATE TABLE IF NOT EXISTS cost_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name   TEXT    NOT NULL,
    window_start TEXT    NOT NULL,
    window_end   TEXT    NOT NULL,
    total_usd    REAL    NOT NULL,
    run_count    INTEGER NOT NULL,
    computed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(skill_name, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name  TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    transition  TEXT NOT NULL,
    operator    TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_events_skill ON lifecycle_events(skill_name);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def default_db_path() -> Path:
    return Path.home() / ".sagaflow" / "portfolio.db"


def init_db(db_path: Path | None = None) -> Path:
    """Create the portfolio database with WAL mode and full schema. Idempotent."""
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA_SQL)
        existing = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (CURRENT_SCHEMA_VERSION,),
            )
        conn.commit()
    finally:
        conn.close()

    log.info("Portfolio DB initialized at %s (schema v%d)", path, CURRENT_SCHEMA_VERSION)
    return path


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection to portfolio.db with WAL mode and row factory."""
    path = db_path or default_db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Portfolio DB not found at {path}. Run 'sagaflow portfolio init' first."
        )
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_exists(db_path: Path | None = None) -> bool:
    path = db_path or default_db_path()
    return path.exists()
