"""Intra-session working memory — offloads tool results from LLM context.

Each sagaflow run gets a working memory store backed by SQLite in the run
directory. Subagents write tool results here (full content), keep only a
one-line summary in conversation context, and recall specific entries when
needed. Multiple subagents in the same run share the same store.

This is the Tier 2 layer in the tiered memory architecture:
  Tier 1: Reasoning context (bounded, in-prompt, expensive)
  Tier 2: Working memory (session-scoped, external, cheap writes/selective reads)
  Tier 3: Shared state (cross-agent coordination via the same store)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key         TEXT PRIMARY KEY,
    agent_role  TEXT NOT NULL,
    content     TEXT NOT NULL,
    summary     TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    byte_size   INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    accessed_at REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    key, summary, content, tags,
    content='entries', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, key, summary, content, tags)
    VALUES (new.rowid, new.key, new.summary, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, key, summary, content, tags)
    VALUES ('delete', old.rowid, old.key, old.summary, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, key, summary, content, tags)
    VALUES ('delete', old.rowid, old.key, old.summary, old.content, old.tags);
    INSERT INTO entries_fts(rowid, key, summary, content, tags)
    VALUES (new.rowid, new.key, new.summary, new.content, new.tags);
END;
"""


@dataclass
class MemoryEntry:
    key: str
    agent_role: str
    content: str
    summary: str
    tags: list[str] = field(default_factory=list)
    byte_size: int = 0
    created_at: float = 0.0
    accessed_at: float | None = None


class WorkingMemory:
    """SQLite-backed working memory for a single sagaflow run."""

    def __init__(self, run_dir: str | Path) -> None:
        self._run_dir = Path(run_dir)
        self._db_path = self._run_dir / "working_memory.db"
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def store(
        self,
        key: str,
        content: str,
        summary: str,
        agent_role: str = "unknown",
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        now = time.time()
        tag_list = tags or []
        entry = MemoryEntry(
            key=key,
            agent_role=agent_role,
            content=content,
            summary=summary,
            tags=tag_list,
            byte_size=len(content.encode("utf-8")),
            created_at=now,
        )
        self._conn.execute(
            """INSERT OR REPLACE INTO entries
            (key, agent_role, content, summary, tags, byte_size, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, agent_role, content, summary, json.dumps(tag_list), entry.byte_size, now),
        )
        self._conn.commit()
        return entry

    def recall(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        now = time.time()
        rows = self._conn.execute(
            """SELECT e.* FROM entries e
            JOIN entries_fts f ON e.rowid = f.rowid
            WHERE entries_fts MATCH ?
            ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
        keys = [r["key"] for r in rows]
        if keys:
            placeholders = ",".join("?" for _ in keys)
            self._conn.execute(
                f"UPDATE entries SET accessed_at = ? WHERE key IN ({placeholders})",
                [now, *keys],
            )
            self._conn.commit()
        return [self._to_entry(r) for r in rows]

    def get(self, key: str) -> MemoryEntry | None:
        row = self._conn.execute(
            "SELECT * FROM entries WHERE key = ?", (key,),
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE entries SET accessed_at = ? WHERE key = ?",
                (time.time(), key),
            )
            self._conn.commit()
            return self._to_entry(row)
        return None

    def list_entries(self, agent_role: str | None = None) -> list[dict]:
        if agent_role:
            rows = self._conn.execute(
                "SELECT key, agent_role, summary, byte_size, created_at FROM entries WHERE agent_role = ? ORDER BY created_at",
                (agent_role,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT key, agent_role, summary, byte_size, created_at FROM entries ORDER BY created_at",
            ).fetchall()
        return [
            {
                "key": r["key"],
                "agent_role": r["agent_role"],
                "summary": r["summary"],
                "byte_size": r["byte_size"],
            }
            for r in rows
        ]

    def stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) as count, COALESCE(SUM(byte_size), 0) as total_bytes FROM entries",
        ).fetchone()
        return {
            "entry_count": row["count"],
            "total_bytes": row["total_bytes"],
            "db_path": str(self._db_path),
        }

    def close(self) -> None:
        self._conn.close()

    def _to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        tags = json.loads(row["tags"]) if row["tags"] else []
        return MemoryEntry(
            key=row["key"],
            agent_role=row["agent_role"],
            content=row["content"],
            summary=row["summary"],
            tags=tags,
            byte_size=row["byte_size"],
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
        )
