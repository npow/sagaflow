"""Research tools callable from the RLM WASM sandbox.

These functions run on the host Python process and are exposed to the DSPy RLM
sandbox as callable tools. The LLM writes code that calls them by name; results
stay in sandbox local variables (never entering the LLM's context window).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SRC_TIMEOUT = 30


def search_codebase(*, query: str, repo: str = "", count: int = 10) -> str:
    """Search Netflix codebase via Sourcegraph.

    Args:
        query: Search query (code, filenames, symbols).
        repo: Optional repo filter like "github.netflix.net/corp/my-repo".
        count: Max results to return.

    Returns:
        Formatted search results with file paths and matching content.
    """
    sq = query
    if repo:
        sq = f"repo:{repo} {query}"

    try:
        result = subprocess.run(
            ["src", "search", "-json", sq],
            capture_output=True,
            text=True,
            timeout=_SRC_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"[search_codebase error: {exc}]"

    if result.returncode != 0:
        return f"[search_codebase error: exit {result.returncode}: {result.stderr[:500]}]"

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return f"[search_codebase: invalid JSON response, length={len(result.stdout)}]"

    results = data.get("Results", [])[:count]
    if not results:
        return f"[search_codebase: no results for '{query}']"

    lines: list[str] = []
    for r in results:
        typename = r.get("__typename", "")
        if typename == "FileMatch":
            finfo = r.get("file", {})
            path = finfo.get("path", "?")
            url = finfo.get("url", "")
            matches = r.get("lineMatches", [])
            lines.append(f"## {path}")
            if url:
                lines.append(f"URL: sourcegraph.netflix.net{url}")
            for m in matches[:5]:
                preview = m.get("preview", "").strip()
                lineno = m.get("lineNumber", "?")
                lines.append(f"  L{lineno}: {preview}")
            lines.append("")

    return "\n".join(lines) if lines else f"[search_codebase: {len(results)} results but no file matches]"


def search_docs(*, query: str, size: int = 5) -> str:
    """Search Netflix internal documentation via DGW RAG (manuals namespace).

    Args:
        query: Natural language search query.
        size: Max results to return.

    Returns:
        Relevant documentation excerpts.
    """
    from urllib.parse import quote_plus

    url = (
        "https://dgwrag.vip.us-east-1.prod.cloud.netflix.net:7004"
        f"/v1/namespaces/manuals/doc?query_str={quote_plus(query)}&size={size}"
    )

    try:
        result = subprocess.run(
            [
                "metatron", "curl",
                "-a", "dgwrag",
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", "{}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=_SRC_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"[search_docs error: {exc}]"

    if result.returncode != 0:
        return f"[search_docs error: exit {result.returncode}: {result.stderr[:300]}]"

    try:
        data = json.loads(result.stdout)
        results = data.get("results", [])
        if not results:
            return f"[search_docs: no results for '{query}']"
        texts = [r.get("text", "") for r in results if r.get("text")]
        return "\n---\n".join(texts)[:8000]
    except (json.JSONDecodeError, TypeError):
        return result.stdout[:8000] if result.stdout else "[search_docs: empty response]"


def search_slack(*, query: str, size: int = 5) -> str:
    """Search Netflix Slack history via DGW RAG (slack namespace).

    Args:
        query: Natural language search query.
        size: Max results to return.

    Returns:
        Relevant Slack conversation excerpts.
    """
    from urllib.parse import quote_plus

    url = (
        "https://dgwrag.vip.us-east-1.prod.cloud.netflix.net:7004"
        f"/v1/namespaces/slack/doc?query_str={quote_plus(query)}&size={size}"
    )

    try:
        result = subprocess.run(
            [
                "metatron", "curl",
                "-a", "dgwrag",
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", "{}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=_SRC_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"[search_slack error: {exc}]"

    if result.returncode != 0:
        return f"[search_slack error: exit {result.returncode}: {result.stderr[:300]}]"

    try:
        data = json.loads(result.stdout)
        results = data.get("results", [])
        if not results:
            return f"[search_slack: no results for '{query}']"
        texts = [r.get("text", "") for r in results if r.get("text")]
        return "\n---\n".join(texts)[:8000]
    except (json.JSONDecodeError, TypeError):
        return result.stdout[:8000] if result.stdout else "[search_slack: empty response]"


def read_file(*, path: str, max_lines: int = 200) -> str:
    """Read a local file.

    Args:
        path: Absolute file path.
        max_lines: Maximum lines to read.

    Returns:
        File contents (truncated if necessary).
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"[read_file: {path} does not exist]"
        if not p.is_file():
            return f"[read_file: {path} is not a file]"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
        return "\n".join(lines)
    except Exception as exc:
        return f"[read_file error: {exc}]"


ALL_TOOLS = [search_codebase, search_docs, search_slack, read_file]
