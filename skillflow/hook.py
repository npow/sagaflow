"""SessionStart hook installer + session-start context formatter."""

from __future__ import annotations

import json
import os
from pathlib import Path

from skillflow.inbox import Inbox

HOOK_COMMAND = "skillflow hook session-start"
HOOK_EVENT = "SessionStart"


def _default_settings_path() -> Path:
    return Path(os.environ["HOME"]) / ".claude" / "settings.json"


def _load(path: Path) -> dict:  # type: ignore[type-arg]
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:  # type: ignore[type-arg]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_installed(*, settings_path: Path | None = None) -> bool:
    path = settings_path or _default_settings_path()
    data = _load(path)
    for event_list in data.get("hooks", {}).values():
        for entry in event_list:
            if entry.get("command") == HOOK_COMMAND:
                return True
    return False


def install(*, settings_path: Path | None = None) -> None:
    path = settings_path or _default_settings_path()
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    event_list = hooks.setdefault(HOOK_EVENT, [])
    for entry in event_list:
        if entry.get("command") == HOOK_COMMAND:
            return
    event_list.append({"command": HOOK_COMMAND})
    _write(path, data)


def uninstall(*, settings_path: Path | None = None) -> None:
    path = settings_path or _default_settings_path()
    data = _load(path)
    hooks = data.get("hooks", {})
    if HOOK_EVENT not in hooks:
        return
    hooks[HOOK_EVENT] = [
        entry for entry in hooks[HOOK_EVENT] if entry.get("command") != HOOK_COMMAND
    ]
    if not hooks[HOOK_EVENT]:
        del hooks[HOOK_EVENT]
    _write(path, data)


def format_session_start_context(*, inbox: Inbox) -> str:
    entries = inbox.unread()
    if not entries:
        return ""
    lines = ["Unread skillflow runs:"]
    for e in entries:
        lines.append(
            f"- {e.run_id} {e.status} {e.skill}  {e.summary}"
            f"  (skillflow show {e.run_id})"
        )
    return "\n".join(lines) + "\n"
