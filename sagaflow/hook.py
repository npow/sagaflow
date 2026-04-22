"""SessionStart hook installer + session-start context formatter.

Claude Code's hook schema groups commands under a matcher:

    {
      "hooks": {
        "SessionStart": [
          {
            "matcher": "",
            "hooks": [
              {"type": "command", "command": "sagaflow hook session-start"}
            ]
          }
        ]
      }
    }

This module preserves any existing matcher groups or non-sagaflow commands
in the same groups and only adds/removes the sagaflow command entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sagaflow.inbox import Inbox

HOOK_COMMAND = "sagaflow hook session-start"
HOOK_EVENT = "SessionStart"
HOOK_MATCHER = ""  # empty matcher = match everything (Claude Code convention)


def _default_settings_path() -> Path:
    return Path(os.environ["HOME"]) / ".claude" / "settings.json"


def _load(path: Path) -> dict:  # type: ignore[type-arg]
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:  # type: ignore[type-arg]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _inner_hooks(group: dict) -> list[dict]:  # type: ignore[type-arg]
    inner = group.get("hooks")
    return inner if isinstance(inner, list) else []


def is_installed(*, settings_path: Path | None = None) -> bool:
    path = settings_path or _default_settings_path()
    data = _load(path)
    for event_list in data.get("hooks", {}).values():
        if not isinstance(event_list, list):
            continue
        for group in event_list:
            if not isinstance(group, dict):
                continue
            for cmd in _inner_hooks(group):
                if isinstance(cmd, dict) and cmd.get("command") == HOOK_COMMAND:
                    return True
    return False


def install(*, settings_path: Path | None = None) -> None:
    path = settings_path or _default_settings_path()
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    event_list = hooks.setdefault(HOOK_EVENT, [])

    # If our command is already in any matcher group, no-op.
    for group in event_list:
        if not isinstance(group, dict):
            continue
        for cmd in _inner_hooks(group):
            if isinstance(cmd, dict) and cmd.get("command") == HOOK_COMMAND:
                return

    # Prefer to append into an existing empty-matcher group if one exists;
    # otherwise create a new one. This keeps the file tidy when other skills
    # share the same matcher.
    #
    # IMPORTANT: require BOTH keys to be schema-correct (matcher is a string,
    # hooks is a list) before reusing. A legacy/corrupt entry without a
    # matcher key would otherwise get implicitly treated as matcher="" and
    # we would inject our command into a malformed neighbor.
    our_entry = {"type": "command", "command": HOOK_COMMAND}
    for group in event_list:
        if (
            isinstance(group, dict)
            and isinstance(group.get("matcher"), str)
            and group["matcher"] == HOOK_MATCHER
            and isinstance(group.get("hooks"), list)
        ):
            group["hooks"].append(our_entry)
            _write(path, data)
            return

    event_list.append({"matcher": HOOK_MATCHER, "hooks": [our_entry]})
    _write(path, data)


def uninstall(*, settings_path: Path | None = None) -> None:
    path = settings_path or _default_settings_path()
    data = _load(path)
    hooks = data.get("hooks", {})
    if HOOK_EVENT not in hooks or not isinstance(hooks[HOOK_EVENT], list):
        return

    kept_groups: list[dict] = []  # type: ignore[type-arg]
    for group in hooks[HOOK_EVENT]:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        inner = _inner_hooks(group)
        kept_inner = [
            cmd for cmd in inner
            if not (isinstance(cmd, dict) and cmd.get("command") == HOOK_COMMAND)
        ]
        if not kept_inner:
            continue  # drop the whole group if empty after removing our command
        new_group = dict(group)
        new_group["hooks"] = kept_inner
        kept_groups.append(new_group)

    if kept_groups:
        hooks[HOOK_EVENT] = kept_groups
    else:
        del hooks[HOOK_EVENT]
    _write(path, data)


def format_session_start_context(*, inbox: Inbox) -> str:
    entries = inbox.unread()
    if not entries:
        return ""
    lines = ["Unread sagaflow runs:"]
    for e in entries:
        lines.append(
            f"- {e.run_id} {e.status} {e.skill}  {e.summary}"
            f"  (sagaflow show {e.run_id})"
        )
    return "\n".join(lines) + "\n"
