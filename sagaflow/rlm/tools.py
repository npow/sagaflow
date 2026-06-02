"""Research tools callable from the RLM WASM sandbox.

These functions run on the host Python process and are exposed to the DSPy RLM
sandbox as callable tools. The LLM writes code that calls them by name; results
stay in sandbox local variables (never entering the LLM's context window).

Tools are plain Python functions with keyword-only args returning str. Additional
tools can be registered via:
  1. Python entry points (group ``sagaflow.rlm.tools``)
  2. A module path in the ``SAGAFLOW_RLM_TOOLS`` environment variable
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RlmTool = Callable[..., str]


# ---------------------------------------------------------------------------
# Built-in tools (generic, no vendor-specific dependencies)
# ---------------------------------------------------------------------------

def read_file(*, path: str, max_lines: int = 200) -> str:
    """Read a local file.

    Args:
        path: Absolute file path.
        max_lines: Maximum lines to read.

    Returns:
        File contents with stable 1-based line numbers, truncated if necessary.
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"[read_file: {path} does not exist]"
        if not p.is_file():
            return f"[read_file: {path} is not a file]"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = lines[:max_lines]
        numbered = [f"{i}:{line}" for i, line in enumerate(kept, 1)]
        if len(lines) > max_lines:
            numbered.append(f"... ({len(lines) - max_lines} more lines)")
        return "\n".join(numbered)
    except Exception as exc:
        return f"[read_file error: {exc}]"


BUILTIN_TOOLS: list[RlmTool] = [read_file]


# ---------------------------------------------------------------------------
# Plugin discovery
# ---------------------------------------------------------------------------

def _load_entry_point_tools() -> list[RlmTool]:
    """Load tools registered under the ``sagaflow.rlm.tools`` entry-point group."""
    tools: list[RlmTool] = []
    try:
        if sys.version_info >= (3, 12):
            from importlib.metadata import entry_points
            eps = entry_points(group="sagaflow.rlm.tools")
        else:
            from importlib.metadata import entry_points
            all_eps = entry_points()
            eps = all_eps.get("sagaflow.rlm.tools", [])

        for ep in eps:
            try:
                obj = ep.load()
                if callable(obj):
                    tools.append(obj)
                elif isinstance(obj, list):
                    tools.extend(t for t in obj if callable(t))
                else:
                    logger.warning("Entry point %s is not callable or list, skipping", ep.name)
            except Exception:
                logger.warning("Failed to load entry point %s", ep.name, exc_info=True)
    except Exception:
        logger.debug("Entry point discovery unavailable", exc_info=True)
    return tools


def _load_module_tools(module_path: str) -> list[RlmTool]:
    """Load tools from ``module`` or ``module:attr``.

    Two accepted forms:
    - ``sagaflow_nflx.rlm_tools`` — imports the module, looks for ``TOOLS``.
    - ``sagaflow_nflx.rlm_tools:CUSTOM`` — imports the module, looks for ``CUSTOM``.

    Either form must point at a list of callables.
    """
    if ":" in module_path:
        mod_name, attr_name = module_path.split(":", 1)
    else:
        mod_name, attr_name = module_path, "TOOLS"
    try:
        mod = importlib.import_module(mod_name)
        tools_list: Any = getattr(mod, attr_name, None)
        if tools_list is None:
            logger.warning("Module %s has no %s list", mod_name, attr_name)
            return []
        return [t for t in tools_list if callable(t)]
    except Exception:
        logger.warning("Failed to import tools module %s", module_path, exc_info=True)
        return []


def discover_tools() -> list[RlmTool]:
    """Return all available RLM tools: built-in + entry points + env var module."""
    tools = list(BUILTIN_TOOLS)

    ep_tools = _load_entry_point_tools()
    if ep_tools:
        logger.info("Loaded %d tool(s) from entry points", len(ep_tools))
        tools.extend(ep_tools)

    module_path = os.environ.get("SAGAFLOW_RLM_TOOLS")
    if module_path:
        mod_tools = _load_module_tools(module_path)
        if mod_tools:
            logger.info("Loaded %d tool(s) from %s", len(mod_tools), module_path)
            tools.extend(mod_tools)

    return tools
