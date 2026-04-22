import json
from datetime import datetime

from skillflow.hook import (
    HOOK_COMMAND,
    HOOK_EVENT,
    HOOK_MATCHER,
    format_session_start_context,
    install,
    is_installed,
    uninstall,
)
from skillflow.inbox import InboxEntry, Inbox


def _find_our_commands(data: dict) -> list[dict]:  # type: ignore[type-arg]
    """Walk the schema and return every inner-hook dict whose command is ours."""

    found: list[dict] = []  # type: ignore[type-arg]
    for event_list in data.get("hooks", {}).values():
        for group in event_list:
            for cmd in group.get("hooks", []):
                if cmd.get("command") == HOOK_COMMAND:
                    found.append(cmd)
    return found


def test_install_adds_hook_entry_with_correct_schema(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    install(settings_path=settings)

    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"  # unchanged

    # Event list exists with at least one matcher group.
    groups = data["hooks"][HOOK_EVENT]
    assert isinstance(groups, list) and groups

    # Our command lives inside group["hooks"], not at the group level.
    our_cmds = _find_our_commands(data)
    assert len(our_cmds) == 1
    assert our_cmds[0] == {"type": "command", "command": HOOK_COMMAND}

    # The enclosing group has the expected matcher shape.
    group = next(g for g in groups if any(h.get("command") == HOOK_COMMAND for h in g.get("hooks", [])))
    assert group.get("matcher") == HOOK_MATCHER
    assert isinstance(group.get("hooks"), list)


def test_install_is_idempotent(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    install(settings_path=settings)
    data = json.loads(settings.read_text())
    assert len(_find_our_commands(data)) == 1


def test_install_creates_file_if_missing(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    assert settings.exists()
    assert is_installed(settings_path=settings)


def test_install_preserves_sibling_commands_in_same_matcher(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    # Pre-seed: another skill installed a SessionStart hook under the empty matcher.
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "other-skill hook"}]
                }
            ]
        }
    }))
    install(settings_path=settings)

    data = json.loads(settings.read_text())
    groups = data["hooks"]["SessionStart"]
    assert len(groups) == 1  # reused the existing empty-matcher group
    inner = groups[0]["hooks"]
    commands = [h["command"] for h in inner]
    assert "other-skill hook" in commands
    assert HOOK_COMMAND in commands


def test_uninstall_removes_hook(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    uninstall(settings_path=settings)
    assert not is_installed(settings_path=settings)


def test_uninstall_preserves_sibling_commands(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "other-skill hook"}]
                }
            ]
        }
    }))
    install(settings_path=settings)
    uninstall(settings_path=settings)

    data = json.loads(settings.read_text())
    # The other command's group still exists; our command is gone.
    assert not is_installed(settings_path=settings)
    other_cmds = [
        h["command"]
        for g in data["hooks"]["SessionStart"]
        for h in g["hooks"]
    ]
    assert other_cmds == ["other-skill hook"]


def test_uninstall_noop_when_absent(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    uninstall(settings_path=settings)  # must not raise


def test_format_session_start_context_empty(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    assert format_session_start_context(inbox=inbox) == ""


def test_format_session_start_context_with_entries(tmp_path) -> None:
    inbox = Inbox(path=tmp_path / "INBOX.md")
    inbox.append(
        InboxEntry(
            run_id="r1",
            skill="hello-world",
            status="DONE",
            summary="hi",
            timestamp=datetime(2026, 4, 21, 14, 0, 0),
        )
    )
    ctx = format_session_start_context(inbox=inbox)
    assert "Unread skillflow runs" in ctx
    assert "r1" in ctx
    assert "DONE" in ctx


# ---------------------------------------------------------------------------
# Schema-validation tests
#
# These tests assert the output conforms to Claude Code's hook schema. They
# exist so any future schema drift in our installer — not just the specific
# "flat {command: ...} entry" bug that originally motivated this fix — fails
# fast instead of shipping a settings.json that Claude Code rejects.
#
# Schema reference: https://code.claude.com/docs/en/hooks
# ---------------------------------------------------------------------------


_ALLOWED_HOOK_EVENTS = {
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "Notification",
    "PreCompact",
}


def _assert_valid_hook_schema(data: dict) -> None:  # type: ignore[type-arg]
    """Walk the settings dict and fail on any violation of Claude Code's hook schema.

    This is a defense against a CLASS of bugs where the installer emits JSON
    in a shape Claude Code rejects. Individual schema rules (e.g. matcher
    must be a string, each inner hook must carry {"type": "command", ...})
    are spelled out here so a regression in any one field fails loudly.
    """

    assert isinstance(data, dict), "settings.json must be a JSON object"
    hooks = data.get("hooks")
    if hooks is None:
        return  # absence is valid
    assert isinstance(hooks, dict), "settings['hooks'] must be an object"

    for event_name, event_list in hooks.items():
        assert isinstance(event_name, str) and event_name, (
            f"hook event name must be a non-empty string, got {event_name!r}"
        )
        # We don't fail unknown events — Claude Code may add new ones — but
        # we do assert our own event falls in the allowed set as a sanity check.
        if event_name == HOOK_EVENT:
            assert event_name in _ALLOWED_HOOK_EVENTS, (
                f"skillflow's event {event_name!r} must be a documented Claude Code event"
            )

        assert isinstance(event_list, list), (
            f"hooks[{event_name!r}] must be a list, got {type(event_list).__name__}"
        )

        for i, group in enumerate(event_list):
            assert isinstance(group, dict), (
                f"hooks[{event_name!r}][{i}] must be an object (matcher + inner hooks list); "
                f"got {type(group).__name__}. This is the shape Claude Code rejects if "
                f'it receives a flat {{"command": "..."}} entry directly.'
            )
            assert "matcher" in group, (
                f"hooks[{event_name!r}][{i}] missing required 'matcher' key"
            )
            assert isinstance(group["matcher"], str), (
                f"hooks[{event_name!r}][{i}]['matcher'] must be a string, got {type(group['matcher']).__name__}"
            )
            assert "hooks" in group, (
                f"hooks[{event_name!r}][{i}] missing required inner 'hooks' key — this is "
                f"the exact shape Claude Code complains about with "
                f'"hooks: Expected array, but received undefined".'
            )
            inner = group["hooks"]
            assert isinstance(inner, list), (
                f"hooks[{event_name!r}][{i}]['hooks'] must be a list, got {type(inner).__name__}"
            )
            for j, cmd in enumerate(inner):
                assert isinstance(cmd, dict), (
                    f"hooks[{event_name!r}][{i}]['hooks'][{j}] must be an object"
                )
                assert cmd.get("type") == "command", (
                    f"hooks[{event_name!r}][{i}]['hooks'][{j}]['type'] must equal 'command', "
                    f"got {cmd.get('type')!r}"
                )
                assert isinstance(cmd.get("command"), str) and cmd["command"], (
                    f"hooks[{event_name!r}][{i}]['hooks'][{j}]['command'] must be a non-empty string"
                )


def test_install_output_conforms_to_claude_code_hook_schema(tmp_path) -> None:
    """Install into an empty settings; the result must validate."""

    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    _assert_valid_hook_schema(json.loads(settings.read_text()))


def test_install_result_is_recognized_by_is_installed_roundtrip(tmp_path) -> None:
    """If install emits a shape is_installed can't find, we'd ship a dead hook."""

    settings = tmp_path / "settings.json"
    install(settings_path=settings)
    assert is_installed(settings_path=settings), (
        "is_installed() must recognize the exact shape install() produces — "
        "a divergence means the installer and reader are parsing different schemas."
    )


def test_install_leaves_unrelated_events_untouched(tmp_path) -> None:
    """Installing a SessionStart hook must not modify PostToolUse, Stop, etc."""

    settings = tmp_path / "settings.json"
    pre_existing = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": "echo edited"}],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "other-tool on-stop"}],
                }
            ],
        }
    }
    settings.write_text(json.dumps(pre_existing))

    install(settings_path=settings)
    data = json.loads(settings.read_text())

    # Unrelated events are byte-for-byte preserved.
    assert data["hooks"]["PostToolUse"] == pre_existing["hooks"]["PostToolUse"]
    assert data["hooks"]["Stop"] == pre_existing["hooks"]["Stop"]
    # Our event added cleanly.
    assert HOOK_EVENT in data["hooks"]
    _assert_valid_hook_schema(data)


def test_uninstall_preserves_unrelated_events(tmp_path) -> None:
    """Uninstalling a SessionStart hook must not perturb other events either."""

    settings = tmp_path / "settings.json"
    install(settings_path=settings)

    # Append an unrelated event AFTER install, to simulate a real-world
    # multi-skill config.
    data = json.loads(settings.read_text())
    data["hooks"]["PostToolUse"] = [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash-logger"}]}
    ]
    settings.write_text(json.dumps(data))

    uninstall(settings_path=settings)
    after = json.loads(settings.read_text())

    assert not is_installed(settings_path=settings)
    # PostToolUse is still exactly what we put there.
    assert after["hooks"]["PostToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash-logger"}]}
    ]
    _assert_valid_hook_schema(after)


def test_install_is_stable_under_repeated_cycles(tmp_path) -> None:
    """install/uninstall/install must converge to a stable, valid state each round."""

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    for _ in range(3):
        install(settings_path=settings)
        _assert_valid_hook_schema(json.loads(settings.read_text()))
        uninstall(settings_path=settings)
        _assert_valid_hook_schema(json.loads(settings.read_text()))

    # Final state after the cycles: our hook is uninstalled; 'theme' is intact.
    final = json.loads(settings.read_text())
    assert final.get("theme") == "dark"
    assert not is_installed(settings_path=settings)


def test_install_rejects_injecting_into_malformed_existing_entry(tmp_path) -> None:
    """If a pre-existing entry is itself malformed, we should not silently worsen the file.

    Specifically: the original Task 12 bug wrote entries like {"command": "..."}
    directly under SessionStart[]. If a user's settings already contains one
    of those (from a bad prior install), our fresh install should STILL
    produce a schema-valid file — either by ignoring the malformed sibling
    or by leaving it untouched while inserting our correctly-shaped group.
    """

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                # Legacy / corrupt entry shape — missing 'matcher' and inner 'hooks'.
                {"command": "legacy-broken-entry"}
            ]
        }
    }))

    install(settings_path=settings)
    data = json.loads(settings.read_text())

    # Our command must be present in a valid group.
    assert is_installed(settings_path=settings)
    # Our group (the one we added) must be schema-valid on its own; broken
    # legacy sibling is acceptable as-is (the installer shouldn't mutate
    # things it doesn't own).
    groups = data["hooks"]["SessionStart"]
    our_groups = [
        g for g in groups
        if isinstance(g, dict)
        and isinstance(g.get("hooks"), list)
        and any(isinstance(h, dict) and h.get("command") == HOOK_COMMAND for h in g["hooks"])
    ]
    assert len(our_groups) == 1
    group = our_groups[0]
    assert isinstance(group.get("matcher"), str)
    assert all(
        isinstance(h, dict) and h.get("type") == "command" and isinstance(h.get("command"), str)
        for h in group["hooks"]
    )
