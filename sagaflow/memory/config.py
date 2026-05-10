"""Generate MCP config that includes the working memory server.

Merges the working memory stdio server into whatever MCP config
the skill already specifies, so subagents get both their research
tools AND working memory in a single config.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def build_mcp_config_with_memory(
    run_dir: str | Path,
    agent_role: str = "unknown",
    base_config_path: str | Path | None = None,
) -> Path:
    """Write a merged MCP config to the run directory and return its path.

    If base_config_path is provided, its servers are included alongside
    the working memory server. Otherwise only working memory is configured.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    base_servers: dict = {}
    if base_config_path:
        bp = Path(base_config_path)
        if bp.exists():
            with bp.open() as f:
                base = json.load(f)
            base_servers = base.get("mcpServers", {})

    python = sys.executable
    memory_server = {
        "command": python,
        "args": [
            "-m", "sagaflow.memory.mcp_server",
            "--run-dir", str(run_dir),
            "--agent-role", agent_role,
        ],
        "type": "stdio",
    }

    merged = {
        "mcpServers": {
            **base_servers,
            "working-memory": memory_server,
        }
    }

    config_path = run_dir / f"mcp-config-{agent_role}.json"
    config_path.write_text(json.dumps(merged, indent=2))
    logger.info("Wrote MCP config with working memory: %s", config_path)
    return config_path
