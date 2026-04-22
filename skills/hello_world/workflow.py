"""hello-world: single-activity skill. Proves framework plumbing end-to-end."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.durable.activities import (
        EmitFindingInput,
        SpawnSubagentInput,
        WriteArtifactInput,
    )
    from sagaflow.durable.retry_policies import HAIKU_POLICY


@dataclass(frozen=True)
class HelloWorldInput:
    run_id: str
    name: str
    inbox_path: str
    run_dir: str
    notify: bool = True


def _spawn_greeter(state_run_dir: str, prompt_path: str) -> dict[str, str]:
    """Indirection so tests can monkeypatch without touching Temporal internals."""

    raise NotImplementedError  # workflow uses execute_activity directly; this is a test hook


@workflow.defn(name="HelloWorldWorkflow")
class HelloWorldWorkflow:
    @workflow.run
    async def run(self, inp: HelloWorldInput) -> str:
        prompt_path = f"{inp.run_dir}/prompt.txt"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=prompt_path, content=f"Greet {inp.name}"),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )

        parsed = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role="greeter",
                tier_name="HAIKU",
                system_prompt=(
                    "You are a greeter. Output a greeting using the format "
                    "STRUCTURED_OUTPUT_START / GREETING|<text> / STRUCTURED_OUTPUT_END. "
                    "Do not include any other text."
                ),
                user_prompt_path=prompt_path,
                max_tokens=64,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=180),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=HAIKU_POLICY,
        )

        greeting = parsed.get("GREETING", "hello")
        timestamp = workflow.now().isoformat(timespec="seconds")
        await workflow.execute_activity(
            "emit_finding",
            EmitFindingInput(
                inbox_path=inp.inbox_path,
                run_id=inp.run_id,
                skill="hello-world",
                status="DONE",
                summary=greeting,
                notify=inp.notify,
                timestamp_iso=timestamp,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        return greeting
