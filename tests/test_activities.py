from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sagaflow.durable.activities import (
    EmitFindingInput,
    SpawnSubagentInput,
    WriteArtifactInput,
    emit_finding,
    spawn_subagent,
    write_artifact,
)


async def test_write_artifact_creates_file(tmp_path) -> None:
    target = tmp_path / "subdir" / "out.txt"
    await write_artifact(
        WriteArtifactInput(path=str(target), content="hello")
    )
    assert target.read_text() == "hello"


async def test_emit_finding_appends_inbox_and_notifies(tmp_path) -> None:
    inbox_path = tmp_path / "INBOX.md"
    with patch("sagaflow.durable.activities.notify_desktop") as notif:
        await emit_finding(
            EmitFindingInput(
                inbox_path=str(inbox_path),
                run_id="r1",
                skill="hello-world",
                status="DONE",
                summary="greeted",
                notify=True,
                timestamp_iso="2026-04-21T14:00:00",
            )
        )
    assert "r1" in inbox_path.read_text()
    notif.assert_called_once()


async def test_emit_finding_skips_notification_when_disabled(tmp_path) -> None:
    inbox_path = tmp_path / "INBOX.md"
    with patch("sagaflow.durable.activities.notify_desktop") as notif:
        await emit_finding(
            EmitFindingInput(
                inbox_path=str(inbox_path),
                run_id="r1",
                skill="hello-world",
                status="DONE",
                summary="",
                notify=False,
                timestamp_iso="2026-04-21T14:00:00",
            )
        )
    notif.assert_not_called()


async def test_spawn_subagent_returns_response_via_cli(tmp_path) -> None:
    input_path = tmp_path / "in.txt"
    input_path.write_text("user prompt here")
    cli_call = AsyncMock(
        return_value=MagicMock(
            stdout="The answer is 42.",
            input_tokens=10,
            output_tokens=5,
        )
    )
    fake_cli = MagicMock(call=cli_call)
    with patch("sagaflow.durable.activities._get_cli", return_value=fake_cli):
        parsed = await spawn_subagent(
            SpawnSubagentInput(
                role="greeter",
                tier_name="HAIKU",
                system_prompt="be brief",
                user_prompt_path=str(input_path),
                max_tokens=128,
                tools_needed=False,
            )
        )
    assert parsed["RESPONSE"] == "The answer is 42."
    assert parsed["_input_tokens"] == "10"
    assert parsed["_output_tokens"] == "5"
    cli_call.assert_awaited()


async def test_spawn_subagent_raises_on_missing_input_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        await spawn_subagent(
            SpawnSubagentInput(
                role="greeter",
                tier_name="HAIKU",
                system_prompt="s",
                user_prompt_path=str(tmp_path / "nope.txt"),
                max_tokens=16,
                tools_needed=False,
            )
        )


async def test_spawn_subagent_with_output_schema_returns_parsed_json(tmp_path) -> None:
    from sagaflow.durable.activities import MALFORMED_SENTINEL

    input_path = tmp_path / "in.txt"
    input_path.write_text("user prompt here")
    cli_call = AsyncMock(
        return_value=MagicMock(
            stdout='{"VERDICT": "OK", "REASON": "looks good"}',
            input_tokens=10,
            output_tokens=5,
        )
    )
    fake_cli = MagicMock(call=cli_call)
    with patch("sagaflow.durable.activities._get_cli", return_value=fake_cli):
        parsed = await spawn_subagent(
            SpawnSubagentInput(
                role="critic",
                tier_name="HAIKU",
                system_prompt="be brief",
                user_prompt_path=str(input_path),
                max_tokens=128,
                tools_needed=False,
                output_schema={"type": "object"},
            )
        )
    assert parsed["VERDICT"] == "OK"
    assert parsed["REASON"] == "looks good"
    assert parsed.get(MALFORMED_SENTINEL) is None


async def test_spawn_subagent_returns_sentinel_on_malformed_json(tmp_path) -> None:
    # When output_schema is set but response is not valid JSON, return sentinel.
    from sagaflow.durable.activities import MALFORMED_SENTINEL

    input_path = tmp_path / "in.txt"
    input_path.write_text("user prompt here")
    cli_call = AsyncMock(
        return_value=MagicMock(
            stdout="prose with no JSON at all",
            input_tokens=10,
            output_tokens=5,
        )
    )
    fake_cli = MagicMock(call=cli_call)
    with patch("sagaflow.durable.activities._get_cli", return_value=fake_cli):
        parsed = await spawn_subagent(
            SpawnSubagentInput(
                role="critic",
                tier_name="HAIKU",
                system_prompt="be brief",
                user_prompt_path=str(input_path),
                max_tokens=128,
                tools_needed=False,
                output_schema={"type": "object"},
            )
        )
    assert parsed.get(MALFORMED_SENTINEL) == "1"
    assert "_error" in parsed
    assert "_raw" in parsed


async def test_spawn_subagent_cancels_heartbeat_on_completion(tmp_path) -> None:
    # Regression: background heartbeat loop must be cancelled on activity exit so we
    # don't leak tasks. Verify by patching activity.heartbeat and asserting it is
    # called at most a handful of times for a fast-returning CLI call.
    input_path = tmp_path / "in.txt"
    input_path.write_text("user prompt here")
    cli_call = AsyncMock(
        return_value=MagicMock(
            stdout="hello world",
            input_tokens=1,
            output_tokens=1,
        )
    )
    fake_cli = MagicMock(call=cli_call)
    with (
        patch("sagaflow.durable.activities._get_cli", return_value=fake_cli),
        patch("sagaflow.durable.activities.activity.heartbeat") as beat,
    ):
        parsed = await spawn_subagent(
            SpawnSubagentInput(
                role="critic",
                tier_name="HAIKU",
                system_prompt="s",
                user_prompt_path=str(input_path),
                max_tokens=16,
                tools_needed=False,
            )
        )
    assert parsed["RESPONSE"] == "hello world"
    assert "_input_tokens" in parsed
    # Fast call → heartbeat should fire zero or one time before cancellation.
    assert beat.call_count <= 2
