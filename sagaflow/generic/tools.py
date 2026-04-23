"""Anthropic tool schemas + dispatch table for the sagaflow generic interpreter.

Each tool is a JSON schema Claude sees via the Anthropic API's tool-use feature.
The accompanying ``TOOL_HANDLERS`` table tells the ``ClaudeSkillWorkflow`` how to
dispatch a tool_use block when Claude invokes it: either as a Temporal activity
or as a child workflow.

Descriptions here are instruction-quality because Claude reads them verbatim when
deciding which tool to call. Input schemas use JSON Schema draft-7 conventions
(``type: object``, ``properties``, ``required``).
"""

from __future__ import annotations

from typing import Any

WRITE_ARTIFACT_TOOL: dict[str, Any] = {
    "name": "write_artifact",
    "description": (
        "Write text content to a file under the current run directory. Use this to "
        "persist intermediate artifacts (plans, notes, drafts) or the final report. "
        "The path is interpreted relative to the run_dir; parent directories are "
        "created automatically. Overwrites any existing file at that path."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file to write, either absolute or relative to "
                    "the run directory (e.g. 'report.md', 'notes/step1.md')."
                ),
            },
            "content": {
                "type": "string",
                "description": "Full UTF-8 text content to write to the file.",
            },
        },
        "required": ["path", "content"],
    },
}

READ_FILE_TOOL: dict[str, Any] = {
    "name": "read_file",
    "description": (
        "Read the contents of a file from the repository or the run directory. "
        "Use this to inspect code, prior artifacts, SKILL.md, or any other file "
        "needed to complete the task. Returns the file text and basic metadata. "
        "If a ``working_dir`` is supplied, the path is resolved underneath it and "
        "cannot escape that root via '..' segments."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file. Absolute paths are honored when no "
                    "working_dir is set; otherwise the path is resolved under "
                    "working_dir with '..' escapes rejected."
                ),
            },
            "working_dir": {
                "type": "string",
                "description": (
                    "Optional root directory. When set, the resolved path must "
                    "be contained within it; otherwise the read is rejected."
                ),
            },
            "max_bytes": {
                "type": "integer",
                "description": (
                    "Optional cap on number of bytes returned. Defaults to "
                    "1048576 (1 MiB); longer files are truncated with a marker."
                ),
                "minimum": 1,
            },
        },
        "required": ["path"],
    },
}

BASH_TOOL: dict[str, Any] = {
    "name": "bash",
    "description": (
        "Run a shell command via /bin/sh and capture stdout, stderr, and the exit "
        "code. Use this for build steps, test runs, git operations, and ad-hoc "
        "inspection (ls, head, etc.). A non-zero exit code is reported in the "
        "result rather than raising, so Claude can inspect failures. The command "
        "is killed after ``timeout_seconds`` (default 60s)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute via /bin/sh -c.",
            },
            "working_dir": {
                "type": "string",
                "description": (
                    "Directory the command is executed in. Defaults to the "
                    "process cwd when omitted."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Hard timeout in seconds after which the process is killed. "
                    "Defaults to 60; maximum 600."
                ),
                "minimum": 1,
                "maximum": 600,
            },
        },
        "required": ["command"],
    },
}

GREP_TOOL: dict[str, Any] = {
    "name": "grep",
    "description": (
        "Recursively search for a regex pattern in files using ripgrep. Returns "
        "up to ``max_results`` matching lines with their file path and line "
        "number. Prefer this over bash+grep because it respects .gitignore and "
        "returns a structured payload. Searches relative to ``path`` (defaults to "
        "the current working directory)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Regex pattern to search for (ripgrep / Rust regex syntax)."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "File or directory to search. Defaults to the process "
                    "cwd when omitted."
                ),
            },
            "glob": {
                "type": "string",
                "description": (
                    "Optional glob filter passed to ripgrep via --glob "
                    "(e.g. '*.py' to restrict to Python files)."
                ),
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "When true, pass -i to ripgrep.",
            },
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum number of matching lines to return. Defaults to "
                    "200; maximum 2000."
                ),
                "minimum": 1,
                "maximum": 2000,
            },
        },
        "required": ["pattern"],
    },
}

GLOB_TOOL: dict[str, Any] = {
    "name": "glob",
    "description": (
        "Match files by glob pattern under a root directory, returning absolute "
        "paths. Use this to discover files by name (e.g. 'src/**/*.py'). Symlink "
        "loops are avoided and results are capped at ``max_results`` entries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Glob pattern as understood by pathlib.Path.glob, "
                    "e.g. '**/*.py' or 'tests/*.md'."
                ),
            },
            "root": {
                "type": "string",
                "description": (
                    "Directory to glob from. Defaults to the process cwd."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum number of paths to return. Defaults to 500; "
                    "maximum 5000."
                ),
                "minimum": 1,
                "maximum": 5000,
            },
        },
        "required": ["pattern"],
    },
}

SPAWN_SUBAGENT_TOOL: dict[str, Any] = {
    "name": "spawn_subagent",
    "description": (
        "Spawn a subagent with its own focused conversation to work on a "
        "subtask. The subagent runs in a Temporal child workflow with its own "
        "tool-use loop, so its intermediate steps are independently durable. "
        "Use this for parallelizable work (e.g. critics, judges, researchers) "
        "or for scoping a subtask away from the parent's context. Returns the "
        "subagent's final text output (or structured output if it produced one)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "description": (
                    "Short role label for the subagent (e.g. 'critic', "
                    "'judge', 'researcher'). Appears in logs and reports."
                ),
            },
            "system_prompt": {
                "type": "string",
                "description": (
                    "System prompt defining the subagent's role, tone, and "
                    "constraints."
                ),
            },
            "user_prompt": {
                "type": "string",
                "description": "The concrete subtask for the subagent to work on.",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Subset of tool names (from this registry) the subagent is "
                    "permitted to use. Omit or use an empty list for a "
                    "text-only subagent."
                ),
            },
            "tier_name": {
                "type": "string",
                "enum": ["HAIKU", "SONNET", "OPUS"],
                "description": (
                    "Model tier for the subagent. Default HAIKU; escalate to "
                    "SONNET/OPUS when the subtask warrants deeper reasoning."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "description": (
                    "Hard cap on tool-use iterations for the subagent. "
                    "Defaults to 20."
                ),
                "minimum": 1,
                "maximum": 200,
            },
        },
        "required": ["role", "system_prompt", "user_prompt"],
    },
}

ALL_TOOLS: list[dict[str, Any]] = [
    WRITE_ARTIFACT_TOOL,
    READ_FILE_TOOL,
    BASH_TOOL,
    GREP_TOOL,
    GLOB_TOOL,
    SPAWN_SUBAGENT_TOOL,
]

# Dispatch table. The workflow uses this to decide whether to execute a tool_use
# block as a Temporal activity or as a child workflow. Exactly one of
# ``handler_activity`` / ``handler_child_workflow`` is present per entry.
TOOL_HANDLERS: dict[str, dict[str, str]] = {
    "write_artifact": {"handler_activity": "write_artifact"},
    "read_file": {"handler_activity": "read_file_tool"},
    "bash": {"handler_activity": "bash_tool"},
    "grep": {"handler_activity": "grep_tool"},
    "glob": {"handler_activity": "glob_tool"},
    "spawn_subagent": {"handler_child_workflow": "SubagentWorkflow"},
}

__all__ = [
    "WRITE_ARTIFACT_TOOL",
    "READ_FILE_TOOL",
    "BASH_TOOL",
    "GREP_TOOL",
    "GLOB_TOOL",
    "SPAWN_SUBAGENT_TOOL",
    "ALL_TOOLS",
    "TOOL_HANDLERS",
]
