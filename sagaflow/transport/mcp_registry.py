"""MCP server registry for scoped subprocess configs.

Skills declare MCP dependencies as category names or server names.
The registry resolves these to actual server configs and generates
a filtered .mcp.json for use with `--strict-mcp-config --mcp-config`.

Auto-discovers servers from:
1. Manual registry at ~/.sagaflow/mcp-registry.json (highest priority)
2. Netflix AI Tool Catalog cache at ~/.cache/nflx-ai-catalog/catalogs/
3. Claude global MCP config at ~/.claude/mcp.json
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(os.environ.get(
    "SAGAFLOW_MCP_REGISTRY", os.path.expanduser("~/.sagaflow/mcp-registry.json")
))
_CATALOG_GLOB = os.path.expanduser("~/.cache/nflx-ai-catalog/catalogs/*.json")
_CLAUDE_MCP_PATH = Path(os.path.expanduser("~/.claude/mcp.json"))

_DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "netflix-research": ["core-tools", "jira", "sourcegraph"],
    "netflix-internal": ["core-tools", "jira"],
    "data": ["core-tools", "kragle"],
    "observability": ["core-tools", "observability_metrics"],
    "code-search": ["sourcegraph"],
    "web-only": [],
}

_registry_cache: dict | None = None


def _normalize_name(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def _discover_from_catalogs() -> dict[str, dict]:
    servers: dict[str, dict] = {}
    for f in sorted(glob.glob(_CATALOG_GLOB)):
        try:
            data = json.loads(Path(f).read_text())
        except (json.JSONDecodeError, OSError):
            continue
        mcps = data.get("mcpServers", [])
        if not isinstance(mcps, list):
            continue
        for entry in mcps:
            name = entry.get("name", entry.get("contributor", ""))
            config_str = entry.get("config", "{}")
            try:
                config = json.loads(config_str)
            except json.JSONDecodeError:
                continue
            if name and config:
                normalized = _normalize_name(name)
                servers[normalized] = config
    return servers


def _discover_from_claude_mcp() -> dict[str, dict]:
    if not _CLAUDE_MCP_PATH.exists():
        return {}
    try:
        data = json.loads(_CLAUDE_MCP_PATH.read_text())
        return data.get("mcpServers", {})
    except (json.JSONDecodeError, OSError):
        return {}


def load_registry() -> dict:
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache

    servers: dict[str, dict] = {}
    servers.update(_discover_from_catalogs())
    servers.update(_discover_from_claude_mcp())

    categories = dict(_DEFAULT_CATEGORIES)
    if _REGISTRY_PATH.exists():
        try:
            manual = json.loads(_REGISTRY_PATH.read_text())
            servers.update(manual.get("servers", {}))
            categories.update(manual.get("categories", {}))
        except (json.JSONDecodeError, OSError):
            pass

    _registry_cache = {"servers": servers, "categories": categories}
    logger.info("MCP registry: %d servers, %d categories", len(servers), len(categories))
    return _registry_cache


def resolve_servers(needs: list[str]) -> list[str]:
    registry = load_registry()
    categories = registry.get("categories", _DEFAULT_CATEGORIES)
    servers: set[str] = set()
    for need in needs:
        if need in categories:
            servers.update(categories[need])
        else:
            servers.add(need)
    return sorted(servers)


def generate_mcp_config(server_names: list[str], run_dir: str | None = None) -> str | None:
    """Write a .mcp.json with only the requested servers. Returns the file path."""
    if not server_names:
        return None
    registry = load_registry()
    all_servers = registry.get("servers", {})
    filtered = {name: all_servers[name] for name in server_names if name in all_servers}
    if not filtered:
        return None
    config = {"mcpServers": filtered}
    if run_dir:
        path = os.path.join(run_dir, ".mcp-scoped.json")
    else:
        fd, path = tempfile.mkstemp(suffix=".mcp.json", prefix="sagaflow-")
        os.close(fd)
    Path(path).write_text(json.dumps(config, indent=2))
    return path


def resolve_and_generate(needs: list[str], run_dir: str | None = None) -> str | None:
    """Convenience: resolve categories → servers → write config file."""
    servers = resolve_servers(needs)
    return generate_mcp_config(servers, run_dir=run_dir)
