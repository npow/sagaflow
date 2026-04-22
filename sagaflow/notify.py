"""Desktop notification with silent fallback on failure."""

from __future__ import annotations

import subprocess
import sys

_PLATFORM = sys.platform


def _macos_command(*, title: str, body: str) -> list[str]:
    title_esc = title.replace('"', '\\"')
    body_esc = body.replace('"', '\\"')
    script = f'display notification "{body_esc}" with title "{title_esc}"'
    return ["osascript", "-e", script]


def _linux_command(*, title: str, body: str) -> list[str]:
    return ["notify-send", title, body]


def notify_desktop(*, title: str, body: str) -> None:
    if _PLATFORM == "darwin":
        cmd = _macos_command(title=title, body=body)
    elif _PLATFORM.startswith("linux"):
        cmd = _linux_command(title=title, body=body)
    else:
        return
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return
