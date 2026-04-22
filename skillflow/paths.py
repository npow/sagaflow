"""Filesystem layout for skillflow runtime state."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """Resolves every skillflow filesystem location from a single root."""

    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "INBOX.md"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def worker_log_dir(self) -> Path:
        return self.root / "logs"

    def run_dir_for(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def ensure(self) -> None:
        """Create any missing directories. Idempotent."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.worker_log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Paths:
        override = os.environ.get("SKILLFLOW_ROOT")
        if override:
            return cls(root=Path(override))
        return cls(root=Path(os.environ["HOME"]) / ".skillflow")
