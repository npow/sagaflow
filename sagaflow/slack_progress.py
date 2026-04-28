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

# Approximate cost per million tokens by model prefix.
_COST_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus":   (15.0, 75.0),
    "claude-sonnet": (3.0,  15.0),
    "claude-haiku":  (0.25, 1.25),
}


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
    total_cost_usd: float = 0.0
    total_elapsed_s: float = 0.0


@dataclass(frozen=True)
class DeliverArtifactInput:
    run_dir: str
    artifact_path: str
    comment: str = ""


@dataclass(frozen=True)
class ReportSlackFailureInput:
    run_dir: str
    skill: str
    error: str
    failed_step: str = ""


@dataclass(frozen=True)
class ReportSlackStateChangeInput:
    run_dir: str
    skill_name: str
    state: str  # PAUSED, RUNNING, TAKEOVER
    phase: str = ""
    run_id: str = ""


def init_progress_file(
    run_dir: str | Path,
    channel: str,
    thread_ts: str | None = None,
    *,
    skill_name: str = "",
    run_id: str = "",
) -> Path:
    """Write Slack routing file. Auto-creates a thread if *thread_ts* is ``None``."""
    if not thread_ts and channel:
        label = skill_name or run_id or "sagaflow"
        starter_ts = _slack_post(
            channel, None, f":rocket: *{label}* run started"
        )
        if starter_ts:
            thread_ts = starter_ts

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


def _render(
    title: str,
    steps: list[SlackProgressStep],
    final: bool,
    *,
    total_cost_usd: float = 0.0,
    total_elapsed_s: float = 0.0,
) -> str:
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
    footer_parts = [f"{completed}/{len(steps)} phases"]
    if in_progress:
        footer_parts.append(f"{in_progress} running")
    if total_elapsed_s > 0:
        footer_parts.append(_fmt_duration(total_elapsed_s))
    if total_cost_usd > 0:
        footer_parts.append(f"${total_cost_usd:.2f}")
    lines.extend(["", f"_{' · '.join(footer_parts)}_"])
    return "\n".join(lines)


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s:02d}s"


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "") -> float:
    for prefix, (inp_rate, out_rate) in _COST_PER_MTOK.items():
        if prefix in model:
            return (input_tokens * inp_rate + output_tokens * out_rate) / 1_000_000
    return (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000


def _slack_post(channel: str, thread_ts: str | None, text: str) -> str | None:
    body: dict = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    result = _slack_api("chat.postMessage", body)
    return result.get("ts")


def _slack_update(channel: str, ts: str, text: str) -> None:
    _slack_api("chat.update", {"channel": channel, "ts": ts, "text": text})


def _slack_upload(channel: str, thread_ts: str | None, filepath: str, comment: str = "") -> bool:
    """Upload a file to Slack using the v2 upload flow."""
    path = Path(filepath)
    if not path.exists():
        return False
    size = path.stat().st_size
    # Step 1: get upload URL
    resp = _slack_api("files.getUploadURLExternal", {
        "filename": path.name, "length": size,
    })
    upload_url = resp.get("upload_url")
    file_id = resp.get("file_id")
    if not upload_url or not file_id:
        return False
    # Step 2: upload file content
    try:
        subprocess.run(
            ["curl", "-s", "-F", f"file=@{filepath}", upload_url],
            capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    # Step 3: complete upload
    file_entry: dict = {"id": file_id}
    if comment:
        file_entry["title"] = comment
    complete_body: dict = {"files": [file_entry], "channel_id": channel}
    if thread_ts:
        complete_body["thread_ts"] = thread_ts
    if comment:
        complete_body["initial_comment"] = comment
    _slack_api("files.completeUploadExternal", complete_body)
    return True


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


_STATE_MESSAGES: dict[str, tuple[str, str]] = {
    "PAUSED": (
        ":double_vertical_bar:",
        "paused at {phase}\nUse `sagaflow resume {run_id}` to continue"
        "\nor `sagaflow inject {run_id} --message \"...\"` to add context",
    ),
    "TAKEOVER": (
        ":video_game:",
        "operator takeover\nPhase: {phase}\nOperator is driving. Autonomous execution suspended.",
    ),
    "RUNNING": (
        ":arrow_forward:",
        "resumed\nContinuing from {phase}",
    ),
}


@activity.defn(name="report_slack_state_change")
async def report_slack_state_change(inp: ReportSlackStateChangeInput) -> None:
    """Post a state-change notification to the Slack progress thread."""
    config = _read_progress_file(inp.run_dir)
    if config is None:
        return

    channel = config.get("channel", "")
    thread_ts = config.get("thread_ts")
    if not channel:
        return

    icon, template = _STATE_MESSAGES.get(inp.state, (":question:", "{state}"))
    body = template.format(
        phase=inp.phase or "unknown",
        run_id=inp.run_id or "???",
        state=inp.state,
    )
    text = f"{icon} *{inp.skill_name}* {body}"
    _slack_post(channel, thread_ts, text)


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
    text = _render(
        inp.title, steps, inp.final,
        total_cost_usd=inp.total_cost_usd,
        total_elapsed_s=inp.total_elapsed_s,
    )

    if msg_ts:
        _slack_update(channel, msg_ts, text)
    else:
        new_ts = _slack_post(channel, thread_ts, text)
        if new_ts:
            _write_msg_ts(inp.run_dir, new_ts)


@activity.defn(name="deliver_artifact_to_slack")
async def deliver_artifact_to_slack(inp: DeliverArtifactInput) -> None:
    config = _read_progress_file(inp.run_dir)
    if config is None:
        return

    channel = config.get("channel", "")
    thread_ts = config.get("thread_ts")
    if not channel:
        return

    artifact = Path(inp.artifact_path)
    if not artifact.exists():
        logger.warning("Artifact not found for Slack delivery: %s", artifact)
        return

    _slack_upload(channel, thread_ts, str(artifact), inp.comment)


@activity.defn(name="report_slack_failure")
async def report_slack_failure(inp: ReportSlackFailureInput) -> None:
    config = _read_progress_file(inp.run_dir)
    if config is None:
        return

    channel = config.get("channel", "")
    thread_ts = config.get("thread_ts")
    if not channel:
        return

    parts = [f":x: *{inp.skill}* failed"]
    if inp.failed_step:
        parts.append(f"*Step:* {inp.failed_step}")
    parts.append(f"```{inp.error[:1500]}```")
    _slack_post(channel, thread_ts, "\n".join(parts))
