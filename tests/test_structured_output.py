import pytest

from sagaflow.transport.structured_output import (
    MalformedResponseError,
    parse_structured,
)


def test_parses_single_block() -> None:
    text = """
    Some prose before.
    STRUCTURED_OUTPUT_START
    VERDICT|VERIFIED
    CONFIDENCE|high
    NOTES|Looks correct
    STRUCTURED_OUTPUT_END
    Prose after.
    """
    result = parse_structured(text)
    assert result == {
        "VERDICT": "VERIFIED",
        "CONFIDENCE": "high",
        "NOTES": "Looks correct",
    }


def test_last_block_wins_when_multiple() -> None:
    text = """
    STRUCTURED_OUTPUT_START
    VERDICT|FALSE
    STRUCTURED_OUTPUT_END

    STRUCTURED_OUTPUT_START
    VERDICT|VERIFIED
    STRUCTURED_OUTPUT_END
    """
    assert parse_structured(text) == {"VERDICT": "VERIFIED"}


def test_missing_markers_raises() -> None:
    with pytest.raises(MalformedResponseError):
        parse_structured("just some prose, no markers at all")


def test_empty_block_raises() -> None:
    text = "STRUCTURED_OUTPUT_START\nSTRUCTURED_OUTPUT_END"
    with pytest.raises(MalformedResponseError):
        parse_structured(text)


def test_pipeless_lines_are_continuation_of_previous_key() -> None:
    """Lines without a pipe between two KEY|value lines are continuations."""
    text = """
    STRUCTURED_OUTPUT_START
    VERDICT|VERIFIED
    this line has no pipe and continues VERDICT
    NOTES|ok
    STRUCTURED_OUTPUT_END
    """
    result = parse_structured(text)
    assert result["VERDICT"] == "VERIFIED\nthis line has no pipe and continues VERDICT"
    assert result["NOTES"] == "ok"


def test_pipe_inside_value_is_preserved() -> None:
    text = "STRUCTURED_OUTPUT_START\nNOTES|a|b|c\nSTRUCTURED_OUTPUT_END"
    assert parse_structured(text) == {"NOTES": "a|b|c"}


def test_multiline_plan_value() -> None:
    """PLAN key with multi-line markdown body is fully captured."""
    text = (
        "STRUCTURED_OUTPUT_START\n"
        "PLAN|# My Plan\n"
        "\n"
        "## Step 1\n"
        "Do the thing.\n"
        "\n"
        "## Step 2\n"
        "Verify it works.\n"
        "ACCEPTANCE_CRITERIA|[\"AC-001\"]\n"
        "STRUCTURED_OUTPUT_END"
    )
    result = parse_structured(text)
    assert "## Step 1" in result["PLAN"]
    assert "## Step 2" in result["PLAN"]
    assert "Verify it works." in result["PLAN"]
    assert result["ACCEPTANCE_CRITERIA"] == '["AC-001"]'


def test_multiline_preserves_blank_lines() -> None:
    """Blank lines within a multi-line value are preserved."""
    text = (
        "STRUCTURED_OUTPUT_START\n"
        "PLAN|line one\n"
        "\n"
        "line three\n"
        "VERDICT|OK\n"
        "STRUCTURED_OUTPUT_END"
    )
    result = parse_structured(text)
    assert result["PLAN"] == "line one\n\nline three"
    assert result["VERDICT"] == "OK"
