"""Workflow intervention support: pause, inject, takeover, abort.

Provides ``InterventionMixin`` for bespoke workflows and standalone helpers
used by both generic and bespoke workflow types.

Signal handlers are **pure** — they only set flags or append to lists.
Activity dispatch (Slack notifications) is deferred to ``_intervention_checkpoint``
via the ``_pending_state_notification`` bridge field. This preserves Temporal's
determinism contract: signal handlers are replayed, so they must not await
activities or perform I/O.

State machine::

    RUNNING ──pause──► PAUSED ──resume──► RUNNING
       │                  │                   ▲
       │               takeover               │
       │                  │               release
       │                  ▼                   │
       │              TAKEOVER ───────────────┘
       │                  │
       └──────abort──►  (exit)
"""

from __future__ import annotations

import re
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.slack_progress import ReportSlackStateChangeInput


# ---------------------------------------------------------------------------
# AbortRequestedError — non-retryable activity exception
# ---------------------------------------------------------------------------


class AbortRequestedError(Exception):
    """Raised by activities when the workflow abort flag is set mid-execution.

    Registered as non-retryable so abort latency is bounded even during
    active retry loops.
    """


# ---------------------------------------------------------------------------
# Abort reason sanitization
# ---------------------------------------------------------------------------

_SAFE_CHARS = re.compile(r"[^\w\s.,()/-]")
_MAX_REASON_LEN = 200


def _sanitize_abort_reason(reason: str) -> str:
    """Strip unsafe characters and cap length for log/history safety."""
    cleaned = _SAFE_CHARS.sub("_", reason.strip())
    return cleaned[:_MAX_REASON_LEN]


# ---------------------------------------------------------------------------
# InterventionMixin — for bespoke workflows (deep-qa, deep-design, etc.)
# ---------------------------------------------------------------------------


class InterventionMixin:
    """Mixin providing intervention signals/queries for bespoke workflows.

    IMPORTANT: Call ``_init_intervention(run_dir)`` at the top of your
    ``@workflow.run`` method before any checkpoint call.

    Usage in a bespoke workflow::

        @workflow.defn(name="DeepQAWorkflow")
        class DeepQAWorkflow(InterventionMixin):
            @workflow.run
            async def run(self, inp):
                self._init_intervention(inp.run_dir)
                await self._intervention_checkpoint("Phase 1")
                if self._abort_requested:
                    return "aborted"
                # ... phase 1 work ...
    """

    def _init_intervention(self, run_dir: str, skill_name: str = "") -> None:
        self._intervention_state: str = "RUNNING"
        self._pause_requested: bool = False
        self._takeover_requested: bool = False
        self._abort_requested: bool = False
        self._abort_reason: str = ""
        self._pending_state_notification: str = ""
        self._injected_messages: list[str] = []
        self._human_messages: list[str] = []
        self._current_phase: str = ""
        self._run_dir: str = run_dir
        self._skill_name: str = skill_name

    # ---- Signals (pure: set flags / append only) ----

    @workflow.signal
    async def pause(self) -> None:
        self._pause_requested = True

    @workflow.signal
    async def resume(self) -> None:
        self._intervention_state = "RUNNING"
        self._pause_requested = False
        self._takeover_requested = False
        self._pending_state_notification = "RUNNING"

    @workflow.signal
    async def inject(self, message: str) -> None:
        self._injected_messages.append(message)
        if self._intervention_state == "PAUSED":
            self._intervention_state = "RUNNING"
            self._pause_requested = False
            self._pending_state_notification = "RUNNING"

    @workflow.signal
    async def takeover(self) -> None:
        self._takeover_requested = True
        if self._intervention_state == "PAUSED":
            self._intervention_state = "TAKEOVER"

    @workflow.signal
    async def human_message(self, text: str) -> None:
        self._human_messages.append(text)

    @workflow.signal
    async def release(self) -> None:
        dropped = len(self._human_messages)
        if dropped:
            workflow.logger.warning(
                "release(): discarding %d queued human_message(s)", dropped
            )
        self._human_messages.clear()
        self._takeover_requested = False
        self._intervention_state = "RUNNING"
        self._pending_state_notification = "RUNNING"

    @workflow.signal
    async def abort(self, reason: str = "user-abort") -> None:
        self._abort_requested = True
        self._abort_reason = _sanitize_abort_reason(reason)

    # ---- Queries (pure reads) ----

    @workflow.query
    def get_status(self) -> dict:
        return {
            "intervention_state": self._intervention_state,
            "current_phase": self._current_phase,
            "pause_requested": self._pause_requested,
            "takeover_requested": self._takeover_requested,
            "pending_injections": len(self._injected_messages),
            "run_dir": self._run_dir,
            "skill_name": self._skill_name,
        }

    # ---- Checkpoint logic ----

    async def _intervention_checkpoint(self, phase_name: str) -> None:
        """Call at phase boundaries. Blocks while paused, handles state transitions.

        Must be called from the main workflow coroutine (not from a signal handler).
        """
        assert hasattr(self, "_intervention_state"), (
            "InterventionMixin: call _init_intervention() before _intervention_checkpoint(). "
            "Add self._init_intervention(inp.run_dir) at the top of your @workflow.run method."
        )
        self._current_phase = phase_name

        # Drain any pending state-change notifications from signal handlers.
        await self._flush_state_notification()

        # Check abort first — always takes priority.
        if self._abort_requested:
            return

        # Pause transition: commit the pause.
        if self._pause_requested:
            self._intervention_state = "PAUSED"
            self._pause_requested = False
            self._pending_state_notification = "PAUSED"
            await self._flush_state_notification()

            # Block until resumed, injected into, taken over, or aborted.
            await workflow.wait_condition(
                lambda: (
                    self._intervention_state != "PAUSED"
                    or self._abort_requested
                )
            )
            if self._abort_requested:
                return

        # Takeover transition.
        if self._takeover_requested and self._intervention_state != "TAKEOVER":
            self._intervention_state = "TAKEOVER"
            self._pending_state_notification = "TAKEOVER"
            await self._flush_state_notification()

    async def _flush_state_notification(self) -> None:
        """Dispatch a Slack notification for a pending state change."""
        state = self._pending_state_notification
        if not state:
            return
        self._pending_state_notification = ""
        try:
            await workflow.execute_activity(
                "report_slack_state_change",
                ReportSlackStateChangeInput(
                    run_dir=self._run_dir,
                    skill_name=self._skill_name,
                    state=state,
                    phase=self._current_phase,
                ),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=_NOTIFICATION_POLICY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning("Slack state-change notification failed", exc_info=True)

    def _drain_injections_as_prefix(self) -> str:
        """Drain queued injections into a prompt prefix for the next agent."""
        if not self._injected_messages:
            return ""
        prefix = "\n".join(
            f"[OPERATOR NOTE]: {msg}" for msg in self._injected_messages
        )
        self._injected_messages.clear()
        return prefix + "\n\n"


# Lightweight retry for Slack notifications — best-effort, don't block the workflow.
with workflow.unsafe.imports_passed_through():
    from temporalio.common import RetryPolicy

_NOTIFICATION_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_attempts=2,
    non_retryable_error_types=["AbortRequestedError"],
)
