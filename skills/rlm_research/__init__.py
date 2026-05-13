"""RLM Research skill — multi-RLM fan-out deep research with bounded LLM context.

Runs the multi-RLM orchestrator (sagaflow.rlm.orchestrator) under a durable
Temporal workflow:

    Phase 1  Decompose query into ~6-8 orthogonal dimensions (Sonnet)
    Phase 2  Parallel RLM per dimension — each in its own Deno WASM sandbox
    Phase 3  Synthesize across dimensions and detect gaps (Sonnet)
    Phase 4  Targeted gap-fill RLMs, re-synthesize (up to N rounds)
    Phase 5  Independent verification pass (Haiku)
    Phase 6  Assemble final report

The orchestrator process runs via the ``run_shell`` activity, which heartbeats
while the subprocess is alive so the workflow survives the long wall time
(30-90 min for thorough research).

Usage:
    sagaflow launch rlm-research \\
        --query "How is <subject> used in production?" \\
        --max-dimensions 8 --iters-per-dimension 60 --max-gap-rounds 2

Env vars (must be set on the worker):
    RLM_API_BASE          OpenAI-compatible base URL (e.g. MGP proxy)
    RLM_API_KEY           API key (defaults to "sk-dummy" for MGP proxy)
    SAGAFLOW_RLM_TOOLS    Optional. Two forms accepted:
                          - "sagaflow_nflx.rlm_tools"        (defaults to TOOLS attr)
                          - "sagaflow_nflx.rlm_tools:CUSTOM" (explicit attr name)
                          Either form must point at a list of callables.
                          If unset, only the built-in `read_file` tool is available.
"""

from __future__ import annotations

import json
import shlex
from datetime import timedelta

from sagaflow.durable.activities import (
    RunShellInput,
    RunShellResult,
    run_shell_activity,
)
from sagaflow.durable.helpers import write
from sagaflow.skill import Skill


# Heartbeat-aware long ceiling. The orchestrator caps each subprocess RLM at
# its own internal timeout (typically 5-10 min/dim). Wall time for the
# orchestrator end-to-end is dominated by ``max_dimensions * iters_per_dimension``
# so we set the activity ceiling to 2 hours and let the subprocess return on
# its own.
ACTIVITY_TIMEOUT = timedelta(hours=2)
HEARTBEAT_TIMEOUT = timedelta(seconds=120)
DEFAULT_PYTHON = "/apps/default-python/bin/python3"


class RlmResearch(Skill):
    name = "rlm-research"
    phases = ["Setup", "Research", "Report"]

    async def run(
        self,
        query: str,
        max_dimensions: int = 8,
        # 25 iters captures the convergence sweet spot — measured runs submit
        # at 18-28 iters when satisfied, and the per-iter cost grows with the
        # accumulated trajectory so 50+ rarely buys more substance.
        iters_per_dimension: int = 25,
        llm_calls_per_dimension: int = 60,
        max_gap_rounds: int = 2,
        # 8 workers lets all 8 dims fan out in one batch instead of 2x4 — and
        # the MGP gateway has handled this cleanly during benchmarks.
        max_workers: int = 8,
        verbose: bool = False,
    ) -> str:
        from temporalio import workflow
        from temporalio.common import RetryPolicy

        self.progress(0, "configuring multi-RLM orchestrator")

        cmd_parts = [
            f"export PATH=$HOME/.deno/bin:$PATH &&",
            DEFAULT_PYTHON, "-m", "sagaflow.rlm.orchestrator",
            "--query", shlex.quote(query),
            "--run-dir", shlex.quote(self.run_dir),
            "--max-dimensions", str(max_dimensions),
            "--iters-per-dimension", str(iters_per_dimension),
            "--llm-calls-per-dimension", str(llm_calls_per_dimension),
            "--max-gap-rounds", str(max_gap_rounds),
            "--max-workers", str(max_workers),
            "--python-path", DEFAULT_PYTHON,
        ]
        if verbose:
            cmd_parts.append("--verbose")
        cmd = " ".join(cmd_parts)

        self.progress(1, f"running multi-RLM ({max_dimensions} dimensions)")
        await self._flush_progress()

        # `result_type=RunShellResult` is load-bearing: when execute_activity
        # is called by string name, Temporal returns a raw dict otherwise and
        # `result.exit_code` raises AttributeError.
        result: RunShellResult = await workflow.execute_activity(
            "run_shell",
            RunShellInput(
                command=cmd,
                cwd="/root/projects/sagaflow",
                timeout_seconds=ACTIVITY_TIMEOUT.total_seconds() - 60,
                label=f"rlm-research: {query[:60]}",
            ),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
            result_type=RunShellResult,
        )

        self.progress(2, "writing report")

        if result.exit_code != 0:
            error_detail = (result.stderr or "")[-1000:] or "unknown error"
            await write(
                f"{self.run_dir}/error.txt",
                f"orchestrator failed (exit {result.exit_code}):\n{error_detail}\n\n"
                f"stdout (tail):\n{result.stdout[-2000:]}",
            )
            return f"RLM research failed: {error_detail[-200:]}"

        try:
            last_line = result.stdout.strip().split("\n")[-1]
            output = json.loads(last_line)
            iters = output.get("total_iterations", "?")
            elapsed = output.get("total_elapsed_seconds", "?")
            dims = output.get("dimensions", "?")
            gap_rounds = output.get("gap_rounds", 0)
            err_count = len(output.get("errors", []))
            summary = (
                f"Research complete: {dims} dimensions, {iters} total iters, "
                f"{elapsed}s elapsed"
            )
            if gap_rounds:
                summary += f", {gap_rounds} gap-fill round(s)"
            if err_count:
                summary += f" ({err_count} dimension error(s))"
            return summary
        except (json.JSONDecodeError, IndexError, KeyError):
            return f"Research complete (raw output: {result.stdout[-200:]})"
