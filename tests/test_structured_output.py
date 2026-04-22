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


def test_ignores_lines_without_pipe_separator() -> None:
    text = """
    STRUCTURED_OUTPUT_START
    VERDICT|VERIFIED
    this line has no pipe and must be skipped
    NOTES|ok
    STRUCTURED_OUTPUT_END
    """
    assert parse_structured(text) == {"VERDICT": "VERIFIED", "NOTES": "ok"}


def test_pipe_inside_value_is_preserved() -> None:
    text = "STRUCTURED_OUTPUT_START\nNOTES|a|b|c\nSTRUCTURED_OUTPUT_END"
    assert parse_structured(text) == {"NOTES": "a|b|c"}
