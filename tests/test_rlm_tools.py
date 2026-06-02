"""Tests for generic RLM host tools."""

from __future__ import annotations

from sagaflow.rlm.tools import read_file


def test_read_file_prefixes_lines_with_numbers(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    assert read_file(path=str(source)) == "1:alpha\n2:beta\n3:gamma"


def test_read_file_preserves_truncation_with_numbered_lines(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    assert read_file(path=str(source), max_lines=2) == "1:alpha\n2:beta\n... (1 more lines)"
