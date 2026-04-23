"""Unit tests for sagaflow.generic.tools.

Validate that each tool schema conforms to Anthropic's tool-use API shape
(name / description / input_schema with type=object, properties, required) and
that the ``TOOL_HANDLERS`` dispatch table is well-formed.
"""

from __future__ import annotations

import pytest

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


EXPECTED_NAMES = {
    "write_artifact",
    "read_file",
    "bash",
    "grep",
    "glob",
    "spawn_subagent",
}


def test_all_tools_contains_exactly_six_schemas() -> None:
    assert len(ALL_TOOLS) == 6
    names = {t["name"] for t in ALL_TOOLS}
    assert names == EXPECTED_NAMES


@pytest.mark.parametrize(
    "tool",
    [
        WRITE_ARTIFACT_TOOL,
        READ_FILE_TOOL,
        BASH_TOOL,
        GREP_TOOL,
        GLOB_TOOL,
        SPAWN_SUBAGENT_TOOL,
    ],
    ids=["write_artifact", "read_file", "bash", "grep", "glob", "spawn_subagent"],
)
def test_each_schema_matches_anthropic_tool_use_format(tool) -> None:
    # Required top-level keys for Anthropic tool-use.
    assert set(["name", "description", "input_schema"]).issubset(tool.keys())

    assert isinstance(tool["name"], str) and tool["name"]
    assert isinstance(tool["description"], str) and len(tool["description"]) >= 20

    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict) and schema["properties"]
    assert isinstance(schema["required"], list)
    # Every required field must be declared in properties.
    for req in schema["required"]:
        assert req in schema["properties"], (
            f"tool {tool['name']}: required field {req!r} not in properties"
        )
    # Each property should have a type.
    for name, prop in schema["properties"].items():
        assert "type" in prop, (
            f"tool {tool['name']}: property {name!r} is missing 'type'"
        )


def test_write_artifact_requires_path_and_content() -> None:
    assert set(WRITE_ARTIFACT_TOOL["input_schema"]["required"]) == {"path", "content"}


def test_read_file_requires_path_and_supports_working_dir() -> None:
    assert WRITE_ARTIFACT_TOOL["name"] == "write_artifact"
    assert READ_FILE_TOOL["input_schema"]["required"] == ["path"]
    assert "working_dir" in READ_FILE_TOOL["input_schema"]["properties"]


def test_bash_requires_command_and_caps_timeout() -> None:
    schema = BASH_TOOL["input_schema"]
    assert schema["required"] == ["command"]
    timeout_prop = schema["properties"]["timeout_seconds"]
    assert timeout_prop["type"] == "integer"
    assert timeout_prop.get("maximum") == 600


def test_grep_requires_pattern_and_supports_filters() -> None:
    schema = GREP_TOOL["input_schema"]
    assert schema["required"] == ["pattern"]
    props = schema["properties"]
    assert "glob" in props
    assert "case_insensitive" in props
    assert props["case_insensitive"]["type"] == "boolean"


def test_glob_requires_pattern() -> None:
    schema = GLOB_TOOL["input_schema"]
    assert schema["required"] == ["pattern"]
    assert "root" in schema["properties"]


def test_spawn_subagent_schema_shape() -> None:
    schema = SPAWN_SUBAGENT_TOOL["input_schema"]
    required = set(schema["required"])
    assert {"role", "system_prompt", "user_prompt"}.issubset(required)
    # Tier enum guards against free-form model names.
    tier = schema["properties"]["tier_name"]
    assert set(tier["enum"]) == {"HAIKU", "SONNET", "OPUS"}
    # Tools passed through are a list of strings.
    tools_prop = schema["properties"]["tools"]
    assert tools_prop["type"] == "array"
    assert tools_prop["items"] == {"type": "string"}


def test_tool_handlers_lookup_covers_all_tools() -> None:
    assert set(TOOL_HANDLERS.keys()) == EXPECTED_NAMES
    # Each entry has exactly one of handler_activity / handler_child_workflow.
    for name, entry in TOOL_HANDLERS.items():
        keys = set(entry.keys())
        assert keys in (
            {"handler_activity"},
            {"handler_child_workflow"},
        ), f"{name}: unexpected handler keys {keys}"
        assert isinstance(next(iter(entry.values())), str)


def test_tool_handlers_spawn_subagent_is_child_workflow() -> None:
    assert TOOL_HANDLERS["spawn_subagent"] == {
        "handler_child_workflow": "SubagentWorkflow"
    }
    # Every other tool is an activity (not a child workflow).
    for name in EXPECTED_NAMES - {"spawn_subagent"}:
        entry = TOOL_HANDLERS[name]
        assert "handler_activity" in entry
        assert "handler_child_workflow" not in entry


def test_tool_handlers_reuse_existing_write_artifact_activity() -> None:
    # write_artifact reuses the sagaflow base activity (no new activity name).
    assert TOOL_HANDLERS["write_artifact"] == {"handler_activity": "write_artifact"}


def test_tool_handler_activity_names_match_activity_decl_names() -> None:
    # The tool-handler names must line up with the @activity.defn(name=...) decls
    # in sagaflow.generic.activities; otherwise dispatch will fail at runtime.
    from sagaflow.generic import activities as ga
    from sagaflow.durable.activities import write_artifact as base_write_artifact

    # Each tool-handler activity lookup should resolve to an activity function
    # whose registered name matches.
    expected = {
        "write_artifact": base_write_artifact,
        "read_file_tool": ga.read_file_tool,
        "bash_tool": ga.bash_tool,
        "grep_tool": ga.grep_tool,
        "glob_tool": ga.glob_tool,
    }
    for decl_name, fn in expected.items():
        # temporalio stores the registered name on __temporal_activity_definition
        # (private attr name may vary); fall back to function name if unavailable.
        meta = getattr(fn, "__temporal_activity_definition", None)
        resolved_name = getattr(meta, "name", None) if meta is not None else fn.__name__
        assert resolved_name == decl_name, (
            f"expected activity name {decl_name!r}, got {resolved_name!r}"
        )
