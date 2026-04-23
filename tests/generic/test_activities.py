"""Unit tests for sagaflow.generic.activities tool-handler activities.

Each test silences the background heartbeat so the activity's blocking path
is what's exercised. Paths use the pytest ``tmp_path`` fixture for isolation.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from sagaflow.generic.activities import (
    BashInput,
    GlobInput,
    GrepInput,
    ReadFileInput,
    bash_tool,
    glob_tool,
    grep_tool,
    read_file_tool,
)


@pytest.fixture(autouse=True)
def _silence_heartbeat():
    """Patch ``activity.heartbeat`` so background heartbeat loops are no-ops.

    Matches the pattern in ``tests/test_activities.py::test_spawn_subagent_cancels_heartbeat_on_completion``
    but applied as an autouse fixture for brevity across this module.
    """
    with patch("sagaflow.durable.activities.activity.heartbeat"):
        yield


# ---------------------------------------------------------------------------
# read_file_tool
# ---------------------------------------------------------------------------


async def test_read_file_tool_returns_content_for_existing_file(tmp_path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")

    result = await read_file_tool(ReadFileInput(path=str(f)))

    assert result.content == "hello world"
    assert result.size_bytes == len("hello world")
    assert result.truncated is False
    assert result.path == str(f.resolve())


async def test_read_file_tool_raises_for_missing_path(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        await read_file_tool(ReadFileInput(path=str(tmp_path / "nope.txt")))


async def test_read_file_tool_rejects_directory(tmp_path) -> None:
    with pytest.raises(IsADirectoryError):
        await read_file_tool(ReadFileInput(path=str(tmp_path)))


async def test_read_file_tool_truncates_large_file(tmp_path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("x" * 100, encoding="utf-8")

    result = await read_file_tool(ReadFileInput(path=str(f), max_bytes=10))

    assert result.truncated is True
    assert len(result.content) == 10
    assert result.size_bytes == 100


async def test_read_file_tool_rejects_path_escaping_working_dir(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(PermissionError):
        await read_file_tool(
            ReadFileInput(
                path="../outside.txt",
                working_dir=str(root),
            )
        )


async def test_read_file_tool_allows_relative_inside_working_dir(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_text("ok", encoding="utf-8")

    result = await read_file_tool(
        ReadFileInput(path="inside.txt", working_dir=str(root))
    )

    assert result.content == "ok"


# ---------------------------------------------------------------------------
# bash_tool
# ---------------------------------------------------------------------------


async def test_bash_tool_happy_path_captures_stdout() -> None:
    result = await bash_tool(BashInput(command="echo hello"))

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hello" in result.stdout
    assert result.stderr == ""


async def test_bash_tool_captures_nonzero_exit_code() -> None:
    # /bin/sh -c 'exit 7' => exit_code 7 (and not raised).
    result = await bash_tool(BashInput(command="exit 7"))

    assert result.exit_code == 7
    assert result.timed_out is False


async def test_bash_tool_captures_stderr_and_exit() -> None:
    result = await bash_tool(
        BashInput(command=">&2 echo nope; exit 1")
    )

    assert result.exit_code == 1
    assert "nope" in result.stderr


async def test_bash_tool_enforces_timeout() -> None:
    # sleep longer than the timeout → timed_out flag and exit_code 124.
    result = await bash_tool(
        BashInput(command="sleep 5", timeout_seconds=1)
    )

    assert result.timed_out is True
    assert result.exit_code == 124


async def test_bash_tool_runs_in_working_dir(tmp_path) -> None:
    result = await bash_tool(
        BashInput(command="pwd", working_dir=str(tmp_path))
    )

    assert result.exit_code == 0
    # macOS may resolve /private/var/... for /var/...; compare realpaths.
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(str(tmp_path))


async def test_bash_tool_rejects_missing_working_dir(tmp_path) -> None:
    bogus = tmp_path / "does_not_exist"
    with pytest.raises(NotADirectoryError):
        await bash_tool(
            BashInput(command="echo hi", working_dir=str(bogus))
        )


# ---------------------------------------------------------------------------
# grep_tool
# ---------------------------------------------------------------------------


async def test_grep_tool_finds_matches(tmp_path) -> None:
    (tmp_path / "a.py").write_text("needle here\nnothing\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("another line\nneedle again\n", encoding="utf-8")

    result = await grep_tool(
        GrepInput(pattern="needle", path=str(tmp_path))
    )

    assert len(result.matches) == 2
    lines = {m.line for m in result.matches}
    assert "needle here" in lines
    assert "needle again" in lines


async def test_grep_tool_empty_results_for_no_match(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("nothing interesting", encoding="utf-8")

    result = await grep_tool(
        GrepInput(pattern="nosuchpatternhere", path=str(tmp_path))
    )

    assert result.matches == []
    assert result.truncated is False


async def test_grep_tool_case_insensitive(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("Needle\n", encoding="utf-8")

    hit = await grep_tool(
        GrepInput(pattern="needle", path=str(tmp_path), case_insensitive=True)
    )
    miss = await grep_tool(
        GrepInput(pattern="needle", path=str(tmp_path), case_insensitive=False)
    )

    assert len(hit.matches) == 1
    assert miss.matches == []


async def test_grep_tool_glob_filter(tmp_path) -> None:
    (tmp_path / "a.py").write_text("match\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("match\n", encoding="utf-8")

    result = await grep_tool(
        GrepInput(pattern="match", path=str(tmp_path), glob="*.py")
    )

    assert len(result.matches) == 1
    assert result.matches[0].path.endswith("a.py")


async def test_grep_tool_raises_for_missing_path(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        await grep_tool(
            GrepInput(pattern="x", path=str(tmp_path / "no_such_dir"))
        )


async def test_grep_tool_stdlib_fallback_when_no_ripgrep(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")

    with patch("sagaflow.generic.activities.shutil.which", return_value=None):
        result = await grep_tool(
            GrepInput(pattern="needle", path=str(tmp_path))
        )

    assert result.used_ripgrep is False
    assert len(result.matches) == 1


# ---------------------------------------------------------------------------
# glob_tool
# ---------------------------------------------------------------------------


async def test_glob_tool_finds_matching_paths(tmp_path) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("x", encoding="utf-8")

    result = await glob_tool(
        GlobInput(pattern="**/*.py", root=str(tmp_path))
    )

    names = sorted(os.path.basename(p) for p in result.paths)
    assert names == ["a.py", "c.py"]


async def test_glob_tool_no_matches(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    result = await glob_tool(
        GlobInput(pattern="**/*.py", root=str(tmp_path))
    )

    assert result.paths == []
    assert result.truncated is False


async def test_glob_tool_caps_results(tmp_path) -> None:
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")

    result = await glob_tool(
        GlobInput(pattern="*.txt", root=str(tmp_path), max_results=3)
    )

    assert len(result.paths) == 3
    assert result.truncated is True


async def test_glob_tool_rejects_non_directory_root(tmp_path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        await glob_tool(GlobInput(pattern="*", root=str(f)))
