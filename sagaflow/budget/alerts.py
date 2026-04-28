"""Budget threshold alert integration."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def fire_threshold_alert(
    threshold: float,
    accumulated_cost_usd: float,
    max_cost_usd: float,
    run_id: str,
    slack_channel: str | None,
    slack_thread_ts: str | None,
) -> None:
    """Post a Slack notification when a budget threshold is crossed.

    Fire-and-forget: failures are logged but never re-raised.
    """
    if not slack_channel:
        logger.info(
            "budget alert at %d%% for run %s (no Slack channel configured)",
            int(threshold * 100),
            run_id,
        )
        return

    pct = int(threshold * 100)
    msg = (
        f":moneybag: *Budget Alert* — Run `{run_id}` "
        f"has reached *{pct}%* of its ${max_cost_usd:.2f} budget. "
        f"Current spend: *${accumulated_cost_usd:.4f}*"
    )

    try:
        from sagaflow.slack_progress import _slack_post
        _slack_post(slack_channel, slack_thread_ts, msg)
    except Exception:
        logger.warning("failed to post budget alert to Slack", exc_info=True)
