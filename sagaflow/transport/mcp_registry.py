"""MCP server registry for scoped subprocess configs.

Skills declare MCP dependencies as category names or server names.
The registry resolves these to actual server configs and generates
a filtered .mcp.json for use with `--strict-mcp-config --mcp-config`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_REGISTRY_PATH = Path(os.environ.get(
    "SAGAFLOW_MCP_REGISTRY", os.path.expanduser("~/.sagaflow/mcp-registry.json")
))

_DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "netflix-internal": ["core-tools", "jira"],
    "data": ["core-tools", "kragle", "metaflow"],
    "observability": ["observability_metrics"],
}


def load_registry() -> dict:
    if _REGISTRY_PATH.exists():
        return json.loads(_REGISTRY_PATH.read_text())
    return {"servers": {}, "categories": _DEFAULT_CATEGORIES}


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
