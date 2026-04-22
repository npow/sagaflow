# Writing a new sagaflow skill

Each skill is a Python sub-package under `skills/<name>/`. It must:

1. Define a `@workflow.defn` class that accepts an input dataclass and runs phases via `workflow.execute_activity`.
2. Reuse the base activities (`write_artifact`, `emit_finding`, `spawn_subagent`) from `sagaflow.durable.activities`. Add skill-specific activities only when the base ones can't cover the need.
3. Expose a `register(registry: SkillRegistry) -> None` function that registers a `SkillSpec` with the skill's workflow class and activity list.

## Minimal example

See `skills/hello_world/` for a working skill that exercises every framework surface with ~100 lines of code.

## CLI integration

`sagaflow.cli._start_workflow` has a dispatch table keyed on skill name. Add an entry there for your skill — take args from Click options, build the input dataclass, call `client.start_workflow`.

## State schema

Extend `sagaflow.durable.state.WorkflowState` rather than re-rolling generation counter + cost + terminal-label bookkeeping.
