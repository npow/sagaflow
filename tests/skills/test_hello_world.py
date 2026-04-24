"""Workflow-level test for HelloWorldWorkflow using time-skipped Temporal env.

The spec test's `_spawn_greeter` monkeypatch is dead code (workflow uses
execute_activity, not the indirection). We substitute the `spawn_subagent`
activity itself with a deterministic fake to keep the test hermetic.
"""

from __future__ import annotations

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from sagaflow.durable.activities import emit_finding, write_artifact
from sagaflow.temporal_client import TASK_QUEUE
from skills.hello_world.workflow import HelloWorldInput, HelloWorldWorkflow


@activity.defn(name="spawn_subagent")
async def _fake_spawn_subagent(inp) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {"GREETING": f"hello, {inp.role_name_hint}" if hasattr(inp, "role_name_hint") else "hello, alice"}


async def test_hello_world_round_trips(tmp_path) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[HelloWorldWorkflow],
            activities=[write_artifact, emit_finding, _fake_spawn_subagent],
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "httpx", "anthropic", "sagaflow", "pydantic", "skills", "claude_skill_"
                )
            ),
        ):
            result = await env.client.execute_workflow(
                HelloWorldWorkflow.run,
                HelloWorldInput(
                    run_id="hw-1",
                    name="alice",
                    inbox_path=str(tmp_path / "INBOX.md"),
                    run_dir=str(tmp_path / "runs" / "hw-1"),
                    greeter_system_prompt="You are a greeter.",
                    greeter_user_prompt="Greet alice",
                ),
                id="hw-1",
                task_queue=TASK_QUEUE,
            )
    assert result == "hello, alice"
