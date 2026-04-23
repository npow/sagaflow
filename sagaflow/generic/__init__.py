"""Generic claude-skills interpreter.

A single Temporal workflow that runs any claude-skills ``SKILL.md`` durably by
driving Claude's tool-use loop: each tool Claude invokes is dispatched as its
own Temporal activity (or a child workflow for ``spawn_subagent``), so the
tree of work is step-level durable end-to-end.

This module exposes:

* Tool schemas (``tools.py``) — Anthropic tool-use JSON schemas plus a
  ``TOOL_HANDLERS`` dispatch table telling the workflow how to execute each
  tool (activity vs child workflow).
* Tool activities (``activities.py``) — Temporal activities implementing the
  tools, plus ``call_claude_with_tools`` which issues one Anthropic API call
  with the supplied tool schemas and returns text + tool_use blocks.
"""

from sagaflow.generic.activities import (
    BashInput,
    BashResult,
    CallClaudeInput,
    ClaudeResponse,
    ClaudeToolUse,
    GlobInput,
    GlobResult,
    GrepInput,
    GrepMatch,
    GrepResult,
    ReadFileInput,
    ReadFileResult,
    bash_tool,
    call_claude_with_tools,
    generic_tool_activities,
    glob_tool,
    grep_tool,
    read_file_tool,
)
from sagaflow.generic.tools import (
    ALL_TOOLS,
    BASH_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    READ_FILE_TOOL,
    SPAWN_SUBAGENT_TOOL,
    TOOL_HANDLERS,
    WRITE_ARTIFACT_TOOL,
)
from sagaflow.generic.workflow import (
    ClaudeSkillInput,
    ClaudeSkillWorkflow,
    SubagentInput,
    SubagentWorkflow,
)

__all__ = [
    # Tool schemas + dispatch table.
    "ALL_TOOLS",
    "TOOL_HANDLERS",
    "WRITE_ARTIFACT_TOOL",
    "READ_FILE_TOOL",
    "BASH_TOOL",
    "GREP_TOOL",
    "GLOB_TOOL",
    "SPAWN_SUBAGENT_TOOL",
    # Claude tool-use driver.
    "CallClaudeInput",
    "ClaudeResponse",
    "ClaudeToolUse",
    "call_claude_with_tools",
    # Tool-handler activities + their dataclasses.
    "ReadFileInput",
    "ReadFileResult",
    "read_file_tool",
    "BashInput",
    "BashResult",
    "bash_tool",
    "GrepInput",
    "GrepMatch",
    "GrepResult",
    "grep_tool",
    "GlobInput",
    "GlobResult",
    "glob_tool",
    "generic_tool_activities",
    # Workflows.
    "ClaudeSkillInput",
    "ClaudeSkillWorkflow",
    "SubagentInput",
    "SubagentWorkflow",
]
