"""Pydantic models for the manifest execution schema.

Validated at skill-load time. Stored in SKILL.md under the
``execution:`` YAML frontmatter key.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


class InputParam(BaseModel):
    type: Literal["string", "integer", "boolean", "array", "object"]
    required: bool = False
    default: Any = None
    enum: list[str] | None = None


class ContextLoad(BaseModel):
    load: str
    as_: str = Field(alias="as")
    optional: bool = False

    model_config = {"populate_by_name": True}


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_seconds: float = 2.0
    max_backoff_seconds: float = 60.0


class Gate(BaseModel):
    type: Literal[
        "non_empty", "falsifiability", "rubber_stamp",
        "field_truthy", "min_length", "mode_match", "custom",
    ]
    field: str
    min_hypotheses: int | None = None
    similarity_threshold: float | None = None
    reference_field: str | None = None
    value: str | None = None
    custom_activity: str | None = None


class BaseStep(BaseModel):
    id: str
    type: str
    condition_skip: str | None = None


class PromptStep(BaseStep):
    type: Literal["prompt"] = "prompt"
    prompt: str
    model: str = "claude-sonnet-4-6"
    output: str
    gates: list[Gate] = []
    context_inject: dict[str, str] = {}
    timeout_seconds: int = 300
    retry_policy: RetryPolicy | None = None


class LoopStep(BaseStep):
    type: Literal["loop"] = "loop"
    over: str
    max_iterations: int = 10
    exit_condition: Gate | None = None
    body: list[Step]


class ConditionalStep(BaseStep):
    type: Literal["conditional"] = "conditional"
    condition: Gate
    if_true: list[Step]
    if_false: list[Step] = []


class DispatchStep(BaseStep):
    type: Literal["dispatch"] = "dispatch"
    skill: str
    input_map: dict[str, str]
    output: str
    await_result: bool = True
    timeout_seconds: int = 1800


class ShaLockStep(BaseStep):
    type: Literal["sha_lock"] = "sha_lock"
    field: str
    store_as: str


Step = Union[PromptStep, LoopStep, ConditionalStep, DispatchStep, ShaLockStep]

# Rebuild models that use Step forward reference.
LoopStep.model_rebuild()
ConditionalStep.model_rebuild()


class FanOutStep(BaseStep):
    """Parallel execution of the same prompt across multiple items."""
    type: Literal["fan_out"] = "fan_out"
    prompt: str
    model: str = "claude-sonnet-4-6"
    over: str
    output: str
    max_concurrency: int = 6
    timeout_seconds: int = 300


class OutputSpec(BaseModel):
    primary: str
    metadata: dict[str, str] = {}


class ExecutionManifest(BaseModel):
    interpreter_version: int = 1
    input_schema: dict[str, InputParam] = {}
    context: list[ContextLoad] = []
    steps: list[Step]
    output: OutputSpec

    @model_validator(mode="after")
    def _validate_step_outputs(self) -> ExecutionManifest:
        return self


class SkillFrontmatter(BaseModel):
    name: str
    version: int = 1
    description: str = ""
    tags: list[str] = []
    execution: ExecutionManifest | None = None

    model_config = {"extra": "allow"}
