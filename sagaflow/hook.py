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
from typing import Any, cast

from sagaflow.inbox import Inbox

HOOK_COMMAND = "sagaflow hook session-start"
HOOK_EVENT = "SessionStart"
HOOK_MATCHER = ""  # empty matcher = match everything (Claude Code convention)


def _default_settings_path() -> Path:
    return Path(os.environ["HOME"]) / ".claude" / "settings.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _write(path: Path, data: dict[str, Any]) -> None:
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
    drift = _format_cost_drift_warnings()
    if drift:
        lines.append("")
        lines.append(drift)
    return "\n".join(lines) + "\n"


def _format_cost_drift_warnings() -> str:
    """Surface any recent sagaflow runs whose cost-estimate drifted >20% vs API.

    Reads the per-run ``cost_audit.jsonl`` files written by spawn_subagent
    (sagaflow ≥0.10.16). Returns empty string if no recent runs have drift,
    so the session-start banner stays quiet on the happy path.
    """
    try:
        import json as _json
        import time as _time
        from sagaflow.paths import Paths
        runs_dir = Paths.from_env().runs_dir
        if not runs_dir.exists():
            return ""
        cutoff = _time.time() - 7 * 86400  # last 7 days
        flagged: list[tuple[str, float, float]] = []
        for d in runs_dir.iterdir():
            audit = d / "cost_audit.jsonl"
            if not audit.exists() or audit.stat().st_mtime < cutoff:
                continue
            est = rep = 0.0
            for line in audit.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                    est += float(e.get("estimated_usd", 0.0))
                    rep += float(e.get("reported_usd", 0.0))
                except _json.JSONDecodeError:
                    continue
            if rep > 0.05:  # ignore micro-runs where rounding dominates
                drift = (est - rep) / rep
                if abs(drift) > 0.20:
                    flagged.append((d.name, drift, rep))
        if not flagged:
            return ""
        out = ["Cost-estimate drift detected (run `sagaflow cost audit <run_id>` for detail):"]
        for run_id, drift, rep in sorted(flagged, key=lambda x: -abs(x[1]))[:10]:
            out.append(f"  - {run_id}: drift {drift*100:+.0f}% (reported $${rep:.2f})")
        return "\n".join(out)
    except Exception:
        return ""
