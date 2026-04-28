"""SQLite + WAL + FTS5 skill memory database."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / ".sagaflow" / "memory.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL UNIQUE,
    skill           TEXT NOT NULL,
    terminal_label  TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT NOT NULL,
    duration_s      REAL NOT NULL,
    cost_usd        REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    findings_json   TEXT NOT NULL DEFAULT '{}',
    findings_text   TEXT NOT NULL DEFAULT '',
    input_hash      TEXT,
    run_dir         TEXT NOT NULL,
    primary_artifact TEXT,
    sagaflow_version TEXT,
    skill_commit    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_skill ON outcomes(skill);
CREATE INDEX IF NOT EXISTS idx_outcomes_skill_completed ON outcomes(skill, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_terminal ON outcomes(terminal_label);
CREATE INDEX IF NOT EXISTS idx_outcomes_input_hash ON outcomes(input_hash);

CREATE TABLE IF NOT EXISTS patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    skill           TEXT NOT NULL,
    pattern_type    TEXT NOT NULL,
    pattern_key     TEXT NOT NULL,
    description     TEXT NOT NULL,
    frequency       INTEGER DEFAULT 1,
    first_seen_run  TEXT NOT NULL,
    last_seen_run   TEXT NOT NULL,
    confidence      TEXT DEFAULT 'low',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(skill, pattern_type, pattern_key)
);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS outcomes_fts USING fts5(
    run_id, skill, terminal_label, findings_text,
    content='outcomes', content_rowid='id'
);
"""

_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS outcomes_ai AFTER INSERT ON outcomes BEGIN
    INSERT INTO outcomes_fts(rowid, run_id, skill, terminal_label, findings_text)
    VALUES (new.id, new.run_id, new.skill, new.terminal_label, new.findings_text);
END;

CREATE TRIGGER IF NOT EXISTS outcomes_ad AFTER DELETE ON outcomes BEGIN
    INSERT INTO outcomes_fts(outcomes_fts, rowid, run_id, skill, terminal_label, findings_text)
    VALUES ('delete', old.id, old.run_id, old.skill, old.terminal_label, old.findings_text);
END;

CREATE TRIGGER IF NOT EXISTS outcomes_au AFTER UPDATE ON outcomes BEGIN
    INSERT INTO outcomes_fts(outcomes_fts, rowid, run_id, skill, terminal_label, findings_text)
    VALUES ('delete', old.id, old.run_id, old.skill, old.terminal_label, old.findings_text);
    INSERT INTO outcomes_fts(rowid, run_id, skill, terminal_label, findings_text)
    VALUES (new.id, new.run_id, new.skill, new.terminal_label, new.findings_text);
END;
"""


@dataclass
class OutcomeRecord:
    run_id: str = ""
    skill: str = ""
    terminal_label: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_s: float = 0.0
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    findings_json: str = "{}"
    findings_text: str = ""
    input_hash: str | None = None
    run_dir: str = ""
    primary_artifact: str | None = None
    sagaflow_version: str | None = None
    skill_commit: str | None = None


@dataclass
class PatternRecord:
    skill: str = ""
    pattern_type: str = ""
    pattern_key: str = ""
    description: str = ""
    frequency: int = 1
    first_seen_run: str = ""
    last_seen_run: str = ""
    confidence: str = "low"


class SkillMemoryDB:
    """Thin wrapper around SQLite for skill outcome storage + FTS5 recall."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or _DEFAULT_DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        try:
            self._conn.executescript(_FTS_SQL)
            self._conn.executescript(_FTS_TRIGGERS)
        except sqlite3.OperationalError:
            logger.debug("FTS5 setup skipped (may already exist)", exc_info=True)

    @classmethod
    def open(cls, db_path: Path | None = None) -> SkillMemoryDB:
        return cls(db_path)

    def close(self) -> None:
        self._conn.close()

    def upsert_outcome(self, rec: OutcomeRecord) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO outcomes
            (run_id, skill, terminal_label, started_at, completed_at,
             duration_s, cost_usd, input_tokens, output_tokens,
             findings_json, findings_text, input_hash, run_dir,
             primary_artifact, sagaflow_version, skill_commit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rec.run_id, rec.skill, rec.terminal_label,
                rec.started_at, rec.completed_at, rec.duration_s,
                rec.cost_usd, rec.input_tokens, rec.output_tokens,
                rec.findings_json, rec.findings_text, rec.input_hash,
                rec.run_dir, rec.primary_artifact,
                rec.sagaflow_version, rec.skill_commit,
            ),
        )
        self._conn.commit()

    def query_outcomes(
        self,
        *,
        skill: str | None = None,
        query: str | None = None,
        limit: int = 10,
        max_age_days: int = 90,
        terminal_labels: tuple[str, ...] | None = None,
    ) -> list[OutcomeRecord]:
        if query:
            return self._fts_query(
                query=query, skill=skill, limit=limit,
                max_age_days=max_age_days, terminal_labels=terminal_labels,
            )
        return self._structured_query(
            skill=skill, limit=limit,
            max_age_days=max_age_days, terminal_labels=terminal_labels,
        )

    def _fts_query(
        self,
        query: str,
        skill: str | None,
        limit: int,
        max_age_days: int,
        terminal_labels: tuple[str, ...] | None,
    ) -> list[OutcomeRecord]:
        sql = """
            SELECT o.* FROM outcomes o
            JOIN outcomes_fts f ON o.id = f.rowid
            WHERE outcomes_fts MATCH ?
              AND o.completed_at >= datetime('now', ?)
        """
        params: list[Any] = [query, f"-{max_age_days} days"]
        if skill:
            sql += " AND o.skill = ?"
            params.append(skill)
        if terminal_labels:
            placeholders = ",".join("?" for _ in terminal_labels)
            sql += f" AND o.terminal_label IN ({placeholders})"
            params.extend(terminal_labels)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _structured_query(
        self,
        skill: str | None,
        limit: int,
        max_age_days: int,
        terminal_labels: tuple[str, ...] | None,
    ) -> list[OutcomeRecord]:
        sql = "SELECT * FROM outcomes WHERE completed_at >= datetime('now', ?)"
        params: list[Any] = [f"-{max_age_days} days"]
        if skill:
            sql += " AND skill = ?"
            params.append(skill)
        if terminal_labels:
            placeholders = ",".join("?" for _ in terminal_labels)
            sql += f" AND terminal_label IN ({placeholders})"
            params.extend(terminal_labels)
        sql += " ORDER BY completed_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_outcome(self, run_id: str) -> OutcomeRecord | None:
        row = self._conn.execute(
            "SELECT * FROM outcomes WHERE run_id = ?", (run_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_outcomes(self, *, skill: str | None = None, limit: int = 20) -> list[OutcomeRecord]:
        if skill:
            rows = self._conn.execute(
                "SELECT * FROM outcomes WHERE skill = ? ORDER BY completed_at DESC LIMIT ?",
                (skill, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM outcomes ORDER BY completed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count_outcomes(self, skill: str | None = None) -> int:
        if skill:
            row = self._conn.execute("SELECT COUNT(*) FROM outcomes WHERE skill = ?", (skill,)).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()
        return row[0] if row else 0

    def delete_expired(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM outcomes WHERE expires_at IS NOT NULL AND expires_at < datetime('now')",
        )
        self._conn.commit()
        return cur.rowcount

    def _row_to_record(self, row: sqlite3.Row) -> OutcomeRecord:
        return OutcomeRecord(
            run_id=row["run_id"],
            skill=row["skill"],
            terminal_label=row["terminal_label"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            duration_s=row["duration_s"],
            cost_usd=row["cost_usd"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            findings_json=row["findings_json"],
            findings_text=row["findings_text"],
            input_hash=row["input_hash"],
            run_dir=row["run_dir"],
            primary_artifact=row["primary_artifact"],
            sagaflow_version=row["sagaflow_version"],
            skill_commit=row["skill_commit"],
        )

    def upsert_pattern(self, pat: PatternRecord) -> None:
        self._conn.execute(
            """INSERT INTO patterns
            (skill, pattern_type, pattern_key, description,
             frequency, first_seen_run, last_seen_run, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill, pattern_type, pattern_key) DO UPDATE SET
                description = excluded.description,
                frequency = patterns.frequency + 1,
                last_seen_run = excluded.last_seen_run,
                confidence = excluded.confidence,
                updated_at = datetime('now')
            """,
            (
                pat.skill, pat.pattern_type, pat.pattern_key,
                pat.description, pat.frequency,
                pat.first_seen_run, pat.last_seen_run, pat.confidence,
            ),
        )
        self._conn.commit()

    def query_patterns(self, skill: str | None = None, min_frequency: int = 1) -> list[PatternRecord]:
        sql = "SELECT * FROM patterns WHERE frequency >= ?"
        params: list[Any] = [min_frequency]
        if skill:
            sql += " AND skill = ?"
            params.append(skill)
        sql += " ORDER BY frequency DESC, updated_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            PatternRecord(
                skill=r["skill"], pattern_type=r["pattern_type"],
                pattern_key=r["pattern_key"], description=r["description"],
                frequency=r["frequency"], first_seen_run=r["first_seen_run"],
                last_seen_run=r["last_seen_run"], confidence=r["confidence"],
            )
            for r in rows
        ]
