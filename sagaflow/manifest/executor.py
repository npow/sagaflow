"""Interpreter-agnostic execution engine.

Callers provide model_call and dispatch_call as async callables so this
class has no Temporal or Claude Code imports. Both ManifestWorkflow and
ManifestInterpreter create an instance with their own implementations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from sagaflow.manifest.context import ExecutionContext
from sagaflow.manifest.gates import GateError, GateEvaluator
from sagaflow.manifest.prompts import PromptRegistry
from sagaflow.manifest.schema import (
    ConditionalStep,
    DispatchStep,
    ExecutionManifest,
    InputParam,
    LoopStep,
    PromptStep,
    ShaLockStep,
    Step,
)

_log = logging.getLogger(__name__)

ModelCallable = Callable[[str, str, dict[str, Any]], Awaitable[str]]
DispatchCallable = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ManifestExecutor:

    def __init__(
        self,
        manifest: ExecutionManifest,
        skill_root: Path,
        model_call: ModelCallable,
        dispatch_call: DispatchCallable,
    ) -> None:
        self.manifest = manifest
        self.prompts = PromptRegistry(skill_root)
        self.gates = GateEvaluator()
        self.model_call = model_call
        self.dispatch_call = dispatch_call

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        validated = self._validate_inputs(inputs)
        ctx = ExecutionContext(validated)
        self._load_context(ctx)
        for step in self.manifest.steps:
            await self._execute_step(step, ctx)
        return self._collect_output(ctx)

    def _validate_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, param in self.manifest.input_schema.items():
            if name in inputs:
                value = self._coerce_type(name, inputs[name], param)
                if param.enum is not None and value not in param.enum:
                    raise ValueError(
                        f"Input {name!r} value {value!r} not in allowed values: {param.enum}"
                    )
                result[name] = value
            elif param.default is not None:
                result[name] = param.default
            elif param.required:
                raise ValueError(f"Required input missing: {name}")
        return result

    def _coerce_type(self, name: str, value: Any, param: InputParam) -> Any:
        type_map: dict[str, type] = {
            "string": str,
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        expected = type_map.get(param.type)
        if expected is None or isinstance(value, expected):
            return value
        try:
            return expected(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Input {name!r} cannot be coerced to {param.type}: {value!r}"
            ) from exc

    def _load_context(self, ctx: ExecutionContext) -> None:
        for load in self.manifest.context:
            content = self.prompts.get(load.load, ctx.snapshot(), optional=load.optional)
            ctx.set(load.as_, content)

    async def _execute_step(self, step: Step, ctx: ExecutionContext) -> None:
        if step.condition_skip and not ctx.get(step.condition_skip):
            return
        match step:
            case PromptStep():
                await self._exec_prompt(step, ctx)
            case LoopStep():
                await self._exec_loop(step, ctx)
            case ConditionalStep():
                await self._exec_conditional(step, ctx)
            case DispatchStep():
                await self._exec_dispatch(step, ctx)
            case ShaLockStep():
                self._exec_sha_lock(step, ctx)

    async def _exec_prompt(self, step: PromptStep, ctx: ExecutionContext) -> None:
        inject = ctx.resolve_map(step.context_inject)
        merged_ctx = {**ctx.snapshot(), **inject}
        prompt_text = self.prompts.get(step.prompt, merged_ctx)
        system = ctx.get("system_prompt") or ""

        result = await self.model_call(
            system,
            prompt_text,
            {"model": step.model, "timeout_seconds": step.timeout_seconds},
        )

        temp_ctx = ctx.branch()
        temp_ctx.set(step.output, result)
        for gate in step.gates:
            if not self.gates.evaluate(gate, temp_ctx):
                raise GateError(f"Gate {gate.type!r} failed on step {step.id!r}")

        ctx.set(step.output, result)

    async def _exec_loop(self, step: LoopStep, ctx: ExecutionContext) -> None:
        items = ctx.get(step.over)
        if not isinstance(items, list):
            items = list(items) if items else []

        results: list[Any] = []
        for i, item in enumerate(items):
            if i >= step.max_iterations:
                break

            iter_ctx = ctx.branch()
            iter_ctx.set("loop_item", item)
            iter_ctx.set("loop_index", i)

            for body_step in step.body:
                await self._execute_step(body_step, iter_ctx)

            processed_item = iter_ctx.get("loop_item")
            results.append(processed_item)

            if step.exit_condition and self.gates.evaluate(step.exit_condition, iter_ctx):
                break

        ctx.set(f"{step.id}_results", results)

    async def _exec_conditional(self, step: ConditionalStep, ctx: ExecutionContext) -> None:
        branch = step.if_true if self.gates.evaluate(step.condition, ctx) else step.if_false
        for sub_step in branch:
            await self._execute_step(sub_step, ctx)

    async def _exec_dispatch(self, step: DispatchStep, ctx: ExecutionContext) -> None:
        mapped_inputs = ctx.resolve_map(step.input_map)
        if step.await_result:
            try:
                result = await asyncio.wait_for(
                    self.dispatch_call(step.skill, mapped_inputs),
                    timeout=step.timeout_seconds,
                )
            except asyncio.TimeoutError:
                raise GateError(
                    f"Dispatch to skill {step.skill!r} timed out after {step.timeout_seconds}s"
                ) from None
            ctx.set(step.output, result)
        else:
            asyncio.create_task(self.dispatch_call(step.skill, mapped_inputs))
            ctx.set(step.output, None)

    def _exec_sha_lock(self, step: ShaLockStep, ctx: ExecutionContext) -> None:
        value = ctx.get(step.field)
        sha = hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()
        ).hexdigest()
        ctx.set(step.store_as, sha)

    def _collect_output(self, ctx: ExecutionContext) -> dict[str, Any]:
        spec = self.manifest.output
        result: dict[str, Any] = {"result": ctx.get(spec.primary)}
        for key, path in spec.metadata.items():
            result[key] = ctx.get(path)
        return result
