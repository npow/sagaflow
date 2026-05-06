from sagaflow.missions.activities.spawn_subagent import _build_argv


def test_build_argv_uses_bypass_permissions() -> None:
    argv = _build_argv("session-123", "do work")

    assert argv[:3] == ["claude", "--session-id", "session-123"]
    assert "--permission-mode" in argv
    assert "bypassPermissions" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_build_argv_keeps_model_before_prompt() -> None:
    argv = _build_argv("session-123", "do work", model="sonnet")

    assert argv[:3] == ["claude", "--session-id", "session-123"]
    assert "--permission-mode" in argv
    assert "bypassPermissions" in argv
    assert argv[-3:] == ["--model", "sonnet", "do work"]
