"""Tests for sagaflow.replay — cassette record/load/replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sagaflow.replay.cassette import (
    Cassette,
    CassetteEntry,
    cassette_path,
    hash_input,
    list_cassettes,
    load,
    record_entry,
    save,
)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "test-run-20260428-120000"
    d.mkdir()
    return d


class TestCassettePath:
    def test_returns_json_file(self, run_dir: Path) -> None:
        assert cassette_path(run_dir) == run_dir / ".replay_cassette.json"


class TestHashInput:
    def test_deterministic(self) -> None:
        h1 = hash_input("critic", "You are a critic.", "Review this.")
        h2 = hash_input("critic", "You are a critic.", "Review this.")
        assert h1 == h2

    def test_prefix(self) -> None:
        h = hash_input("role", "sys", "usr")
        assert h.startswith("sha256:")

    def test_different_roles_differ(self) -> None:
        h1 = hash_input("critic", "sys", "usr")
        h2 = hash_input("judge", "sys", "usr")
        assert h1 != h2


class TestRecordAndLoad:
    def test_record_creates_file(self, run_dir: Path) -> None:
        record_entry(
            run_dir=run_dir,
            run_id="test-run",
            skill="hello-world",
            activity_name="spawn_subagent",
            role="greeter",
            tier="HAIKU",
            input_hash="sha256:abc123",
            output={"GREETING": "hello"},
            duration_seconds=1.5,
        )
        assert cassette_path(run_dir).exists()

    def test_record_appends(self, run_dir: Path) -> None:
        for i in range(3):
            record_entry(
                run_dir=run_dir,
                run_id="test-run",
                skill="hello-world",
                activity_name="spawn_subagent",
                role=f"role-{i}",
                tier="HAIKU",
                input_hash=f"sha256:{i}",
                output={"step": str(i)},
                duration_seconds=float(i),
            )
        cassette = load(run_dir)
        assert len(cassette.entries) == 3
        assert cassette.entries[0].role == "role-0"
        assert cassette.entries[2].seq == 2

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load(tmp_path / "nonexistent")

    def test_roundtrip(self, run_dir: Path) -> None:
        cassette = Cassette(
            run_id="test-run",
            skill="deep-qa",
            recorded_at="2026-04-28T12:00:00Z",
            entries=[
                CassetteEntry(
                    seq=0,
                    activity="spawn_subagent",
                    role="critic",
                    tier="SONNET",
                    input_hash="sha256:deadbeef",
                    output={"VERDICT": "pass", "_input_tokens": "100"},
                    duration_seconds=5.2,
                ),
            ],
        )
        save(cassette, run_dir)
        loaded = load(run_dir)
        assert loaded.run_id == "test-run"
        assert loaded.skill == "deep-qa"
        assert len(loaded.entries) == 1
        assert loaded.entries[0].output["VERDICT"] == "pass"


class TestSaveLoad:
    def test_save_creates_valid_json(self, run_dir: Path) -> None:
        cassette = Cassette(run_id="r1", skill="s1", recorded_at="now", entries=[])
        p = save(cassette, run_dir)
        data = json.loads(p.read_text())
        assert data["version"] == 1
        assert data["run_id"] == "r1"


class TestListCassettes:
    def test_empty_dir(self, tmp_path: Path) -> None:
        assert list_cassettes(tmp_path) == []

    def test_finds_cassettes(self, tmp_path: Path) -> None:
        for name in ["run-a", "run-b"]:
            d = tmp_path / name
            d.mkdir()
            save(Cassette(run_id=name, skill="test", recorded_at="now", entries=[]), d)
        result = list_cassettes(tmp_path)
        assert len(result) == 2
        ids = {r["run_id"] for r in result}
        assert "run-a" in ids
        assert "run-b" in ids

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert list_cassettes(tmp_path / "nope") == []
