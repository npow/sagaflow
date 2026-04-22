"""hello-world skill registration."""

from __future__ import annotations

from skillflow.durable.activities import emit_finding, spawn_subagent, write_artifact
from skillflow.registry import SkillRegistry, SkillSpec

from skills.hello_world.workflow import HelloWorldWorkflow


def register(registry: SkillRegistry) -> None:
    registry.register(
        SkillSpec(
            name="hello-world",
            workflow_cls=HelloWorldWorkflow,
            activities=[write_artifact, emit_finding, spawn_subagent],
        )
    )
