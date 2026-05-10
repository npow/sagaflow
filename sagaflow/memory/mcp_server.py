"""MCP stdio server exposing working memory to sagaflow subagents.

Run as: python -m sagaflow.memory.mcp_server --run-dir /path/to/run

Provides three tools:
  memory_store   — write content with a summary (offload from context)
  memory_recall  — FTS5 search over stored entries
  memory_list    — index of all entries (keys + summaries, lightweight)

Design: each subagent in a run connects to its own MCP process, but all
processes share the same SQLite database (WAL mode handles concurrency).
"""

from __future__ import annotations

import argparse
import json
import logging

logger = logging.getLogger(__name__)


def _build_server(run_dir: str, agent_role: str = "unknown"):
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ImportError:
        logger.error("mcp package not installed — pip install mcp")
        raise

    from sagaflow.memory.working import WorkingMemory

    mem = WorkingMemory(run_dir)
    server = Server("sagaflow-working-memory")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="memory_store",
                description=(
                    "Store content in working memory. Use this to offload tool results "
                    "from your conversation context. Write the full content here and keep "
                    "only the summary in your response."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Unique identifier (e.g. 'sourcegraph-temporal-usage-1')",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full content to store (tool results, search output, etc.)",
                        },
                        "summary": {
                            "type": "string",
                            "description": "One-line summary to keep in your context index",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags for filtering",
                        },
                    },
                    "required": ["key", "content", "summary"],
                },
            ),
            Tool(
                name="memory_recall",
                description=(
                    "Search working memory for previously stored entries. "
                    "Returns full content of matching entries. Use when you need "
                    "to reference prior findings without them being in your context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (FTS5 — supports AND, OR, NOT, phrase matching)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results to return (default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="memory_list",
                description=(
                    "List all entries in working memory (keys and summaries only — lightweight). "
                    "Use this to see what's been stored without loading full content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "agent_role": {
                            "type": "string",
                            "description": "Filter by agent role (optional)",
                        },
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "memory_store":
            entry = mem.store(
                key=arguments["key"],
                content=arguments["content"],
                summary=arguments["summary"],
                agent_role=agent_role,
                tags=arguments.get("tags"),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "stored": entry.key,
                    "byte_size": entry.byte_size,
                    "summary": entry.summary,
                }),
            )]

        elif name == "memory_recall":
            entries = mem.recall(
                query=arguments["query"],
                limit=arguments.get("limit", 5),
            )
            if not entries:
                return [TextContent(type="text", text="No matching entries found.")]
            results = []
            for e in entries:
                results.append({
                    "key": e.key,
                    "agent_role": e.agent_role,
                    "summary": e.summary,
                    "content": e.content,
                    "byte_size": e.byte_size,
                })
            return [TextContent(type="text", text=json.dumps(results, indent=2))]

        elif name == "memory_list":
            entries = mem.list_entries(agent_role=arguments.get("agent_role"))
            stats = mem.stats()
            return [TextContent(
                type="text",
                text=json.dumps({"entries": entries, "stats": stats}, indent=2),
            )]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return server


async def _run(run_dir: str, agent_role: str) -> None:
    from mcp.server.stdio import stdio_server

    server = _build_server(run_dir, agent_role)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="Sagaflow working memory MCP server")
    parser.add_argument("--run-dir", required=True, help="Path to sagaflow run directory")
    parser.add_argument("--agent-role", default="unknown", help="Role of the connecting agent")
    args = parser.parse_args()

    import asyncio
    asyncio.run(_run(args.run_dir, args.agent_role))


if __name__ == "__main__":
    main()
