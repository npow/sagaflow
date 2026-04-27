"""Slack progress reporting for sagaflow workflows.

Workflows call the ``report_slack_progress`` activity at phase boundaries.
Slack routing (channel, thread_ts) is stored in ``{run_dir}/.slack_progress.json``
— written by the CLI when ``--slack-channel`` is provided.  If the file is absent
the activity silently no-ops, so workflows don't need conditional guards.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity

logger = logging.getLogger(__name__)

SLACK_SCRIPT = os.environ.get(
    "SLACK_SCRIPT",
    os.path.expanduser(
        "~/ws/ngp-skills/plugins/slack-interactions-plugin/"
        "skills/slack-interactions/scripts/slack_request.py"
    ),
)

_PROGRESS_FILE = ".slack_progress.json"


@dataclass
class SlackProgressStep:
    name: str
    status: str = "pending"  # pending | in_progress | completed | error
    detail: str = ""
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class ReportSlackProgressInput:
    run_dir: str
    title: str
    steps: tuple[dict, ...] = ()  # serialized SlackProgressStep dicts
    final: bool = False


def init_progress_file(
    run_dir: str | Path, channel: str, thread_ts: str | None = None
) -> Path:
    path = Path(run_dir) / _PROGRESS_FILE
    path.write_text(
        json.dumps({"channel": channel, "thread_ts": thread_ts, "msg_ts": None}),
        encoding="utf-8",
    )
    return path


def _read_progress_file(run_dir: str) -> dict | None:
    path = Path(run_dir) / _PROGRESS_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_msg_ts(run_dir: str, msg_ts: str) -> None:
    path = Path(run_dir) / _PROGRESS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["msg_ts"] = msg_ts
        path.write_text(json.dumps(data), encoding="utf-8")
    except (json.JSONDecodeError, OSError):
        pass


def _render(title: str, steps: list[SlackProgressStep], final: bool) -> str:
    icon = ":white_check_mark:" if final else ":arrows_counterclockwise:"
    lines = [f"{icon} *{title}*", ""]
    for step in steps:
        if step.status == "completed":
            si = ":white_check_mark:"
        elif step.status == "in_progress":
            si = ":hourglass_flowing_sand:"
        elif step.status == "error":
            si = ":x:"
        else:
            si = ":white_large_square:"
        suffix = ""
        if step.elapsed_s > 0:
            suffix = f" ({_fmt_duration(step.elapsed_s)})"
        detail = f": {step.detail}" if step.detail else ""
        lines.append(f"{si} {step.name}{detail}{suffix}")

    completed = sum(1 for s in steps if s.status == "completed")
    in_progress = sum(1 for s in steps if s.status == "in_progress")
    footer = f"_{completed}/{len(steps)} phases"
    if in_progress:
        footer += f" · {in_progress} running"
    footer += "_"
    lines.extend(["", footer])
    return "\n".join(lines)


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s:02d}s"


def _slack_post(channel: str, thread_ts: str | None, text: str) -> str | None:
    body: dict = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    result = _slack_api("chat.postMessage", body)
    return result.get("ts")


def _slack_update(channel: str, ts: str, text: str) -> None:
    _slack_api("chat.update", {"channel": channel, "ts": ts, "text": text})


def _slack_api(method: str, body: dict) -> dict:
    try:
        result = subprocess.run(
            [SLACK_SCRIPT, "--endpoint", f"/api/{method}", "-X", "POST",
             "--body", json.dumps(body)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return {}


@activity.defn(name="report_slack_progress")
async def report_slack_progress(inp: ReportSlackProgressInput) -> None:
    config = _read_progress_file(inp.run_dir)
    if config is None:
        return

    channel = config.get("channel", "")
    thread_ts = config.get("thread_ts")
    msg_ts = config.get("msg_ts")
    if not channel:
        return

    steps = [SlackProgressStep(**d) for d in inp.steps]
    text = _render(inp.title, steps, inp.final)

    if msg_ts:
        _slack_update(channel, msg_ts, text)
    else:
        new_ts = _slack_post(channel, thread_ts, text)
        if new_ts:
            _write_msg_ts(inp.run_dir, new_ts)
