"""Activities for the generic claude-skills interpreter.

`call_claude_with_tools` is the core Claude-tool-use driver: it issues one
Anthropic API call with the supplied conversation + tool schemas, then extracts
text + tool_use content blocks for the workflow to dispatch.

The tool-handler activities (read_file_tool, bash_tool, grep_tool, glob_tool)
wrap blocking stdlib operations (subprocess, pathlib) with a background
heartbeat loop so Temporal knows the activity is alive during slow commands.
Paths can be scoped to a working_dir where applicable; escapes via ``..``
segments are rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from anthropic import AsyncAnthropic
from temporalio import activity

from sagaflow.durable.activities import _heartbeat_loop
from sagaflow.transport.anthropic_sdk import ModelTier

# Safety caps applied inside the activities regardless of what Claude requests.
_READ_FILE_DEFAULT_MAX_BYTES = 1_048_576  # 1 MiB
_READ_FILE_HARD_MAX_BYTES = 16 * 1_048_576  # 16 MiB
_BASH_DEFAULT_TIMEOUT_SECONDS = 60
_BASH_MAX_TIMEOUT_SECONDS = 600
_GREP_DEFAULT_MAX_RESULTS = 200
_GREP_HARD_MAX_RESULTS = 2000
_GLOB_DEFAULT_MAX_RESULTS = 500
_GLOB_HARD_MAX_RESULTS = 5000


@dataclass(frozen=True)
class CallClaudeInput:
    system_prompt: str
    messages: list[dict]          # [{"role": "user"|"assistant", "content": str | list}]
    tools: list[dict]             # Anthropic tool schemas
    tier_name: str                # "HAIKU" | "SONNET" | "OPUS"
    max_tokens: int = 4096


@dataclass(frozen=True)
class ClaudeToolUse:
    id: str                       # Anthropic's tool_use_id for matching results
    name: str                     # tool name
    input: dict                   # args as a dict


@dataclass(frozen=True)
class ClaudeResponse:
    text: str                     # concatenated text content blocks (may be "")
    tool_uses: list[ClaudeToolUse] = field(default_factory=list)
    stop_reason: str = ""         # "end_turn" | "tool_use" | "max_tokens" | ...
    input_tokens: int = 0
    output_tokens: int = 0


# Tool-passing approach: the existing AnthropicSdkTransport.call() doesn't expose
# a `tools` kwarg, so we talk to `anthropic.AsyncAnthropic` directly here. This
# is less invasive than extending the transport (which only handles non-tool
# single-shot prompts) and keeps the tool-use path self-contained.
def _get_anthropic_client() -> AsyncAnthropic:
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "sk-dummy")
    return AsyncAnthropic(base_url=base_url, api_key=api_key)


def _resolve_tier(tier_name: str) -> ModelTier:
    try:
        return ModelTier[tier_name]
    except KeyError as exc:
        valid = ", ".join(t.name for t in ModelTier)
        raise ValueError(
            f"invalid tier_name {tier_name!r}; must be one of: {valid}"
        ) from exc


def _extract_content(response) -> tuple[str, list[ClaudeToolUse]]:
    text_parts: list[str] = []
    tool_uses: list[ClaudeToolUse] = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif block_type == "tool_use":
            tool_uses.append(
                ClaudeToolUse(
                    id=getattr(block, "id", ""),
                    name=getattr(block, "name", ""),
                    input=dict(getattr(block, "input", {}) or {}),
                )
            )
        # Other content block types (server_tool_use, etc.) are ignored for now.
    return "".join(text_parts), tool_uses


@activity.defn(name="call_claude_with_tools")
async def call_claude_with_tools(inp: CallClaudeInput) -> ClaudeResponse:
    """Call Anthropic with tools, extract text + tool_use blocks.

    Heartbeats every 20s via a background task while the LLM call is in flight,
    matching the `spawn_subagent` pattern so workflows can set a reasonable
    `heartbeat_timeout` without false-tripping on slow LLM responses.
    """
    tier = _resolve_tier(inp.tier_name)
    client = _get_anthropic_client()

    beat_task: asyncio.Task[None] | None = None
    try:
        beat_task = asyncio.create_task(_heartbeat_loop())
    except RuntimeError:
        beat_task = None

    try:
        response = await client.messages.create(
            model=tier.model_id,
            max_tokens=inp.max_tokens,
            system=inp.system_prompt,
            messages=inp.messages,
            tools=inp.tools,
        )
    finally:
        if beat_task is not None:
            beat_task.cancel()
            with contextlib.suppress(BaseException):
                await beat_task

    text, tool_uses = _extract_content(response)
    usage = getattr(response, "usage", None)
    return ClaudeResponse(
        text=text,
        tool_uses=tool_uses,
        stop_reason=getattr(response, "stop_reason", "") or "",
        input_tokens=getattr(usage, "input_tokens", 0) if usage is not None else 0,
        output_tokens=getattr(usage, "output_tokens", 0) if usage is not None else 0,
    )


# ---------------------------------------------------------------------------
# Tool-handler activities: read_file_tool / bash_tool / grep_tool / glob_tool
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _heartbeating():
    """Run a background heartbeat task for the duration of the context."""
    beat_task: asyncio.Task[None] | None = None
    try:
        try:
            beat_task = asyncio.create_task(_heartbeat_loop())
        except RuntimeError:
            beat_task = None
        yield
    finally:
        if beat_task is not None:
            beat_task.cancel()
            with contextlib.suppress(BaseException):
                await beat_task


def _resolve_under_root(path: str, working_dir: str | None) -> Path:
    """Resolve ``path`` and, if ``working_dir`` is set, ensure it stays under it.

    Raises ``PermissionError`` if the resolved path escapes the working_dir.
    """
    candidate = Path(path)
    if working_dir is None:
        return candidate.expanduser().resolve()

    root = Path(working_dir).expanduser().resolve()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.expanduser().resolve()

    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(
            f"Path {path!r} resolves to {resolved} which is outside working_dir {root}"
        ) from exc
    return resolved


@dataclass(frozen=True)
class ReadFileInput:
    path: str
    working_dir: str | None = None
    max_bytes: int | None = None


@dataclass(frozen=True)
class ReadFileResult:
    path: str
    content: str
    size_bytes: int
    truncated: bool


@activity.defn(name="read_file_tool")
async def read_file_tool(inp: ReadFileInput) -> ReadFileResult:
    async with _heartbeating():
        resolved = _resolve_under_root(inp.path, inp.working_dir)
        if not resolved.exists():
            raise FileNotFoundError(f"no such file: {resolved}")
        if not resolved.is_file():
            raise IsADirectoryError(f"not a regular file: {resolved}")

        cap_raw = (
            inp.max_bytes if inp.max_bytes is not None else _READ_FILE_DEFAULT_MAX_BYTES
        )
        if cap_raw < 1:
            cap = _READ_FILE_DEFAULT_MAX_BYTES
        else:
            cap = min(cap_raw, _READ_FILE_HARD_MAX_BYTES)

        # Read up to cap+1 bytes to detect truncation cheaply.
        with resolved.open("rb") as fh:
            raw = fh.read(cap + 1)
        truncated = len(raw) > cap
        if truncated:
            raw = raw[:cap]
        text = raw.decode("utf-8", errors="replace")
        return ReadFileResult(
            path=str(resolved),
            content=text,
            size_bytes=resolved.stat().st_size,
            truncated=truncated,
        )


@dataclass(frozen=True)
class BashInput:
    command: str
    working_dir: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class BashResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    command: str
    working_dir: str | None = None


@activity.defn(name="bash_tool")
async def bash_tool(inp: BashInput) -> BashResult:
    async with _heartbeating():
        raw_timeout = (
            inp.timeout_seconds
            if inp.timeout_seconds is not None
            else _BASH_DEFAULT_TIMEOUT_SECONDS
        )
        timeout = max(1, min(int(raw_timeout), _BASH_MAX_TIMEOUT_SECONDS))
        cwd: str | None = None
        if inp.working_dir is not None:
            cwd_path = Path(inp.working_dir).expanduser().resolve()
            if not cwd_path.is_dir():
                raise NotADirectoryError(f"working_dir not a directory: {cwd_path}")
            cwd = str(cwd_path)

        def _run() -> BashResult:
            try:
                completed = subprocess.run(
                    inp.command,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                return BashResult(
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    exit_code=int(completed.returncode),
                    timed_out=False,
                    command=inp.command,
                    working_dir=cwd,
                )
            except subprocess.TimeoutExpired as exc:
                stdout_data = exc.stdout
                stderr_data = exc.stderr
                if isinstance(stdout_data, bytes):
                    stdout = stdout_data.decode("utf-8", errors="replace")
                else:
                    stdout = stdout_data or ""
                if isinstance(stderr_data, bytes):
                    stderr = stderr_data.decode("utf-8", errors="replace")
                else:
                    stderr = stderr_data or ""
                return BashResult(
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=124,  # conventional timeout exit code
                    timed_out=True,
                    command=inp.command,
                    working_dir=cwd,
                )

        return await asyncio.to_thread(_run)


@dataclass(frozen=True)
class GrepInput:
    pattern: str
    path: str | None = None
    glob: str | None = None
    case_insensitive: bool = False
    max_results: int | None = None


@dataclass(frozen=True)
class GrepMatch:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class GrepResult:
    pattern: str
    matches: list[GrepMatch] = field(default_factory=list)
    truncated: bool = False
    used_ripgrep: bool = False


def _grep_with_ripgrep(
    pattern: str,
    path: str,
    glob_filter: str | None,
    case_insensitive: bool,
    max_results: int,
) -> tuple[list[GrepMatch], bool]:
    rg = shutil.which("rg")
    if rg is None:
        raise FileNotFoundError("ripgrep (rg) is not installed")
    args: list[str] = [rg, "--no-heading", "--line-number", "--with-filename"]
    if case_insensitive:
        args.append("-i")
    if glob_filter:
        args.extend(["--glob", glob_filter])
    args.extend(["--", pattern, path])

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_BASH_MAX_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return [], True

    # rg exit codes: 0 = matches, 1 = no matches, 2 = error.
    if completed.returncode == 1:
        return [], False
    if completed.returncode >= 2:
        raise RuntimeError(f"ripgrep failed: {completed.stderr.strip()}")

    matches: list[GrepMatch] = []
    truncated = False
    for line in completed.stdout.splitlines():
        if len(matches) >= max_results:
            truncated = True
            break
        # Format: path:line_no:content.
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, line_no_str, content = parts
        try:
            line_no = int(line_no_str)
        except ValueError:
            continue
        matches.append(GrepMatch(path=file_path, line_number=line_no, line=content))
    return matches, truncated


def _grep_with_stdlib(
    pattern: str,
    path: str,
    glob_filter: str | None,
    case_insensitive: bool,
    max_results: int,
) -> tuple[list[GrepMatch], bool]:
    flags = re.IGNORECASE if case_insensitive else 0
    compiled = re.compile(pattern, flags)
    root = Path(path)
    files: list[Path]
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = [
            p
            for p in (root.rglob(glob_filter) if glob_filter else root.rglob("*"))
            if p.is_file()
        ]
    else:
        files = []

    matches: list[GrepMatch] = []
    for fp in files:
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, start=1):
                    if compiled.search(line):
                        if len(matches) >= max_results:
                            return matches, True
                        matches.append(
                            GrepMatch(
                                path=str(fp),
                                line_number=i,
                                line=line.rstrip("\n"),
                            )
                        )
        except (OSError, UnicodeDecodeError):
            continue
    return matches, False


@activity.defn(name="grep_tool")
async def grep_tool(inp: GrepInput) -> GrepResult:
    async with _heartbeating():
        raw_max = (
            inp.max_results if inp.max_results is not None else _GREP_DEFAULT_MAX_RESULTS
        )
        max_results = max(1, min(int(raw_max), _GREP_HARD_MAX_RESULTS))
        search_root = inp.path if inp.path is not None else os.getcwd()
        resolved_root = Path(search_root).expanduser().resolve()
        if not resolved_root.exists():
            raise FileNotFoundError(f"grep path does not exist: {resolved_root}")

        use_ripgrep = shutil.which("rg") is not None

        def _run() -> tuple[list[GrepMatch], bool, bool]:
            if use_ripgrep:
                try:
                    matches, truncated = _grep_with_ripgrep(
                        inp.pattern,
                        str(resolved_root),
                        inp.glob,
                        inp.case_insensitive,
                        max_results,
                    )
                    return matches, truncated, True
                except FileNotFoundError:
                    pass
            matches, truncated = _grep_with_stdlib(
                inp.pattern,
                str(resolved_root),
                inp.glob,
                inp.case_insensitive,
                max_results,
            )
            return matches, truncated, False

        matches, truncated, ripgrep_used = await asyncio.to_thread(_run)
        return GrepResult(
            pattern=inp.pattern,
            matches=matches,
            truncated=truncated,
            used_ripgrep=ripgrep_used,
        )


@dataclass(frozen=True)
class GlobInput:
    pattern: str
    root: str | None = None
    max_results: int | None = None


@dataclass(frozen=True)
class GlobResult:
    pattern: str
    root: str
    paths: list[str] = field(default_factory=list)
    truncated: bool = False


@activity.defn(name="glob_tool")
async def glob_tool(inp: GlobInput) -> GlobResult:
    async with _heartbeating():
        raw_max = (
            inp.max_results if inp.max_results is not None else _GLOB_DEFAULT_MAX_RESULTS
        )
        max_results = max(1, min(int(raw_max), _GLOB_HARD_MAX_RESULTS))
        root_str = inp.root if inp.root is not None else os.getcwd()
        root = Path(root_str).expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"glob root is not a directory: {root}")

        def _run() -> tuple[list[str], bool]:
            results: list[str] = []
            truncated = False
            try:
                for p in root.glob(inp.pattern):
                    if len(results) >= max_results:
                        truncated = True
                        break
                    results.append(str(p))
            except (OSError, ValueError):
                return [], False
            return results, truncated

        paths, truncated = await asyncio.to_thread(_run)
        return GlobResult(
            pattern=inp.pattern,
            root=str(root),
            paths=paths,
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# Tool adapter activities
# ---------------------------------------------------------------------------
#
# The generic workflow dispatches each Claude tool_use by name via the
# ``TOOL_HANDLERS`` table. Claude's input is an arbitrary ``dict`` (from the
# JSON tool schema), but the underlying handler activities above take typed
# dataclasses. Rather than marshal dataclasses in workflow code (which can
# trip Temporal's replay determinism when schemas evolve), we expose thin
# ``generic_tool_adapter__<handler_name>`` activities that accept the raw
# dict and reconstruct the dataclass internally. The workflow simply calls
# ``generic_tool_adapter__<name>`` with the dict from Claude.
#
# Each adapter ignores unknown keys so schema drift on Claude's side is
# forward-compatible.


def _filter_kwargs(cls_keys: set[str], args: dict) -> dict:
    return {k: v for k, v in args.items() if k in cls_keys}


@activity.defn(name="generic_tool_adapter__read_file_tool")
async def generic_tool_adapter_read_file_tool(args: dict) -> ReadFileResult:
    kw = _filter_kwargs({"path", "working_dir", "max_bytes"}, args)
    return await read_file_tool(ReadFileInput(**kw))


@activity.defn(name="generic_tool_adapter__bash_tool")
async def generic_tool_adapter_bash_tool(args: dict) -> BashResult:
    kw = _filter_kwargs({"command", "working_dir", "timeout_seconds"}, args)
    return await bash_tool(BashInput(**kw))


@activity.defn(name="generic_tool_adapter__grep_tool")
async def generic_tool_adapter_grep_tool(args: dict) -> GrepResult:
    kw = _filter_kwargs(
        {"pattern", "path", "glob", "case_insensitive", "max_results"}, args
    )
    return await grep_tool(GrepInput(**kw))


@activity.defn(name="generic_tool_adapter__glob_tool")
async def generic_tool_adapter_glob_tool(args: dict) -> GlobResult:
    kw = _filter_kwargs({"pattern", "root", "max_results"}, args)
    return await glob_tool(GlobInput(**kw))


@activity.defn(name="generic_tool_adapter__write_artifact")
async def generic_tool_adapter_write_artifact(args: dict) -> dict[str, str]:
    """Adapter for the base ``write_artifact`` activity.

    Returns a simple status dict so Claude gets confirmation of the write.
    """

    from sagaflow.durable.activities import WriteArtifactInput, write_artifact

    kw = _filter_kwargs({"path", "content"}, args)
    await write_artifact(WriteArtifactInput(**kw))
    return {"status": "ok", "path": kw.get("path", "")}


# Exported list for the worker/registry to pick up.
generic_tool_activities: list[Callable] = [
    read_file_tool,
    bash_tool,
    grep_tool,
    glob_tool,
    generic_tool_adapter_read_file_tool,
    generic_tool_adapter_bash_tool,
    generic_tool_adapter_grep_tool,
    generic_tool_adapter_glob_tool,
    generic_tool_adapter_write_artifact,
]
