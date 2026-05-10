"""Tests for sagaflow.memory.working — intra-session working memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from sagaflow.memory.working import MemoryEntry, WorkingMemory


@pytest.fixture
def mem(tmp_path: Path) -> WorkingMemory:
    m = WorkingMemory(tmp_path / "run-test")
    yield m
    m.close()


class TestStore:
    def test_store_returns_entry(self, mem: WorkingMemory) -> None:
        entry = mem.store(key="k1", content="hello world", summary="greeting")
        assert isinstance(entry, MemoryEntry)
        assert entry.key == "k1"
        assert entry.summary == "greeting"
        assert entry.byte_size == len("hello world".encode())

    def test_store_upserts(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="v1", summary="first")
        mem.store(key="k1", content="v2", summary="second")
        entry = mem.get("k1")
        assert entry is not None
        assert entry.content == "v2"
        assert entry.summary == "second"

    def test_store_with_tags(self, mem: WorkingMemory) -> None:
        entry = mem.store(key="k1", content="x", summary="s", tags=["a", "b"])
        assert entry.tags == ["a", "b"]
        got = mem.get("k1")
        assert got is not None
        assert got.tags == ["a", "b"]

    def test_store_with_agent_role(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="x", summary="s", agent_role="researcher")
        got = mem.get("k1")
        assert got is not None
        assert got.agent_role == "researcher"


class TestGet:
    def test_get_existing(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="data", summary="sum")
        entry = mem.get("k1")
        assert entry is not None
        assert entry.content == "data"

    def test_get_missing(self, mem: WorkingMemory) -> None:
        assert mem.get("nonexistent") is None

    def test_get_updates_accessed_at(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="data", summary="sum")
        mem.get("k1")  # first access sets accessed_at
        entry = mem.get("k1")  # second access reads the updated value
        assert entry is not None
        assert entry.accessed_at is not None


class TestRecall:
    def test_fts_search(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="temporal workflow execution", summary="temporal stuff")
        mem.store(key="k2", content="database connection pooling", summary="db stuff")
        results = mem.recall("temporal")
        assert len(results) == 1
        assert results[0].key == "k1"

    def test_fts_no_match(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="hello", summary="greeting")
        results = mem.recall("xyznonexistent")
        assert results == []

    def test_fts_limit(self, mem: WorkingMemory) -> None:
        for i in range(10):
            mem.store(key=f"k{i}", content=f"common term item {i}", summary=f"item {i}")
        results = mem.recall("common", limit=3)
        assert len(results) == 3

    def test_recall_updates_accessed_at(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="searchable content", summary="sum")
        mem.recall("searchable")  # first access sets accessed_at
        results = mem.recall("searchable")  # second access reads the updated value
        assert len(results) == 1
        assert results[0].accessed_at is not None


class TestListEntries:
    def test_list_all(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="a", summary="s1")
        mem.store(key="k2", content="b", summary="s2")
        entries = mem.list_entries()
        assert len(entries) == 2
        assert {e["key"] for e in entries} == {"k1", "k2"}

    def test_list_by_role(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="a", summary="s1", agent_role="researcher")
        mem.store(key="k2", content="b", summary="s2", agent_role="critic")
        entries = mem.list_entries(agent_role="researcher")
        assert len(entries) == 1
        assert entries[0]["key"] == "k1"

    def test_list_empty(self, mem: WorkingMemory) -> None:
        assert mem.list_entries() == []


class TestStats:
    def test_empty_stats(self, mem: WorkingMemory) -> None:
        stats = mem.stats()
        assert stats["entry_count"] == 0
        assert stats["total_bytes"] == 0

    def test_stats_after_store(self, mem: WorkingMemory) -> None:
        mem.store(key="k1", content="hello", summary="s")
        mem.store(key="k2", content="world!", summary="s")
        stats = mem.stats()
        assert stats["entry_count"] == 2
        assert stats["total_bytes"] == len("hello".encode()) + len("world!".encode())


class TestConfig:
    def test_build_mcp_config(self, tmp_path: Path) -> None:
        from sagaflow.memory.config import build_mcp_config_with_memory
        import json

        run_dir = tmp_path / "run-test"
        config_path = build_mcp_config_with_memory(run_dir, agent_role="researcher")
        assert config_path.exists()
        with config_path.open() as f:
            config = json.load(f)
        assert "working-memory" in config["mcpServers"]
        server = config["mcpServers"]["working-memory"]
        assert server["type"] == "stdio"
        assert "--run-dir" in server["args"]
        assert str(run_dir) in server["args"]

    def test_build_mcp_config_merges_base(self, tmp_path: Path) -> None:
        from sagaflow.memory.config import build_mcp_config_with_memory
        import json

        base_config = tmp_path / "base.json"
        base_config.write_text(json.dumps({
            "mcpServers": {
                "existing-server": {"command": "node", "args": ["server.js"], "type": "stdio"}
            }
        }))
        run_dir = tmp_path / "run-test"
        config_path = build_mcp_config_with_memory(
            run_dir, base_config_path=str(base_config),
        )
        with config_path.open() as f:
            config = json.load(f)
        assert "existing-server" in config["mcpServers"]
        assert "working-memory" in config["mcpServers"]


class TestPromptFragment:
    def test_with_working_memory(self) -> None:
        from sagaflow.memory.prompt_fragment import with_working_memory

        base = "You are a researcher."
        result = with_working_memory(base)
        assert result.startswith("You are a researcher.")
        assert "Working Memory" in result
        assert "memory_store" in result
        assert "memory_recall" in result
