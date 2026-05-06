"""Tests for the manifest execution engine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sagaflow.manifest.context import ExecutionContext
from sagaflow.manifest.executor import ManifestExecutor
from sagaflow.manifest.gates import GateError, GateEvaluator
from sagaflow.manifest.schema import ExecutionManifest, Gate


@pytest.fixture
def simple_manifest() -> ExecutionManifest:
    return ExecutionManifest(
        interpreter_version=1,
        input_schema={"task": {"type": "string", "required": True}},
        steps=[
            {
                "id": "plan",
                "type": "prompt",
                "prompt": "prompts/plan.md",
                "output": "plan_result",
                "gates": [{"type": "non_empty", "field": "plan_result"}],
            },
        ],
        output={"primary": "plan_result"},
    )


@pytest.fixture
def loop_manifest() -> ExecutionManifest:
    return ExecutionManifest(
        interpreter_version=1,
        input_schema={"items": {"type": "array", "required": True}},
        steps=[
            {
                "id": "process_items",
                "type": "loop",
                "over": "inputs.items",
                "max_iterations": 3,
                "body": [
                    {
                        "id": "process_one",
                        "type": "prompt",
                        "prompt": "prompts/process.md",
                        "output": "loop_item.result",
                    },
                ],
            },
        ],
        output={"primary": "process_items_results"},
    )


@pytest.fixture
def conditional_manifest() -> ExecutionManifest:
    return ExecutionManifest(
        interpreter_version=1,
        input_schema={
            "task": {"type": "string", "required": True},
            "mode": {"type": "string", "default": "quick"},
        },
        steps=[
            {
                "id": "route",
                "type": "conditional",
                "condition": {"type": "mode_match", "field": "inputs.mode", "value": "thorough"},
                "if_true": [
                    {
                        "id": "deep",
                        "type": "prompt",
                        "prompt": "prompts/deep.md",
                        "output": "result",
                    },
                ],
                "if_false": [
                    {
                        "id": "quick",
                        "type": "prompt",
                        "prompt": "prompts/quick.md",
                        "output": "result",
                    },
                ],
            },
        ],
        output={"primary": "result"},
    )


@pytest.fixture
def tmp_skill(tmp_path: Path) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "plan.md").write_text("Plan this: $task")
    (prompts / "process.md").write_text("Process item")
    (prompts / "deep.md").write_text("Deep analysis of $task")
    (prompts / "quick.md").write_text("Quick summary of $task")
    return tmp_path


def _make_executor(
    manifest: ExecutionManifest,
    skill_root: Path,
    model_response: str = "mock response",
) -> ManifestExecutor:
    async def mock_model_call(system: str, prompt: str, opts: dict[str, Any]) -> str:
        return model_response

    async def mock_dispatch(skill: str, inputs: dict[str, Any]) -> Any:
        return {"result": f"dispatched to {skill}"}

    return ManifestExecutor(
        manifest=manifest,
        skill_root=skill_root,
        model_call=mock_model_call,
        dispatch_call=mock_dispatch,
    )


class TestExecutionContext:
    def test_get_set(self) -> None:
        ctx = ExecutionContext({"x": 1})
        assert ctx.get("inputs.x") == 1
        ctx.set("foo.bar", "baz")
        assert ctx.get("foo.bar") == "baz"

    def test_branch_isolation(self) -> None:
        ctx = ExecutionContext({"x": 1})
        ctx.set("state", "original")
        branch = ctx.branch()
        branch.set("state", "modified")
        assert ctx.get("state") == "original"
        assert branch.get("state") == "modified"

    def test_resolve_map(self) -> None:
        ctx = ExecutionContext({"a": 1, "b": 2})
        result = ctx.resolve_map({"x": "inputs.a", "y": "inputs.b"})
        assert result == {"x": 1, "y": 2}


class TestGateEvaluator:
    def test_non_empty_pass(self) -> None:
        ctx = ExecutionContext({})
        ctx.set("val", "something")
        gate = Gate(type="non_empty", field="val")
        assert GateEvaluator().evaluate(gate, ctx) is True

    def test_non_empty_fail(self) -> None:
        ctx = ExecutionContext({})
        ctx.set("val", "")
        gate = Gate(type="non_empty", field="val")
        assert GateEvaluator().evaluate(gate, ctx) is False

    def test_mode_match(self) -> None:
        ctx = ExecutionContext({"mode": "thorough"})
        gate = Gate(type="mode_match", field="inputs.mode", value="thorough")
        assert GateEvaluator().evaluate(gate, ctx) is True

    def test_mode_mismatch(self) -> None:
        ctx = ExecutionContext({"mode": "quick"})
        gate = Gate(type="mode_match", field="inputs.mode", value="thorough")
        assert GateEvaluator().evaluate(gate, ctx) is False

    def test_rubber_stamp_novel(self) -> None:
        ctx = ExecutionContext({})
        ctx.set("new", "completely different output with unique words")
        ctx.set("ref", "the original reference text here")
        gate = Gate(type="rubber_stamp", field="new", reference_field="ref", similarity_threshold=0.85)
        assert GateEvaluator().evaluate(gate, ctx) is True

    def test_rubber_stamp_copy(self) -> None:
        ctx = ExecutionContext({})
        ctx.set("new", "the original reference text here")
        ctx.set("ref", "the original reference text here")
        gate = Gate(type="rubber_stamp", field="new", reference_field="ref", similarity_threshold=0.85)
        assert GateEvaluator().evaluate(gate, ctx) is False


class TestManifestExecutor:
    def test_simple_prompt(self, simple_manifest: ExecutionManifest, tmp_skill: Path) -> None:
        executor = _make_executor(simple_manifest, tmp_skill, "the plan is ready")
        result = asyncio.run(executor.execute({"task": "build a widget"}))
        assert result["result"] == "the plan is ready"

    def test_gate_failure(self, simple_manifest: ExecutionManifest, tmp_skill: Path) -> None:
        executor = _make_executor(simple_manifest, tmp_skill, "")
        with pytest.raises(GateError, match="non_empty"):
            asyncio.run(executor.execute({"task": "build a widget"}))

    def test_loop(self, loop_manifest: ExecutionManifest, tmp_skill: Path) -> None:
        executor = _make_executor(loop_manifest, tmp_skill, "processed")
        result = asyncio.run(executor.execute({"items": ["a", "b", "c"]}))
        results = result["result"]
        assert isinstance(results, list)
        assert len(results) == 3

    def test_loop_max_iterations(self, loop_manifest: ExecutionManifest, tmp_skill: Path) -> None:
        executor = _make_executor(loop_manifest, tmp_skill, "processed")
        result = asyncio.run(executor.execute({"items": ["a", "b", "c", "d", "e"]}))
        results = result["result"]
        assert len(results) == 3  # max_iterations=3

    def test_conditional_true_branch(self, conditional_manifest: ExecutionManifest, tmp_skill: Path) -> None:
        executor = _make_executor(conditional_manifest, tmp_skill, "deep result")
        result = asyncio.run(executor.execute({"task": "analyze", "mode": "thorough"}))
        assert result["result"] == "deep result"

    def test_conditional_false_branch(self, conditional_manifest: ExecutionManifest, tmp_skill: Path) -> None:
        executor = _make_executor(conditional_manifest, tmp_skill, "quick result")
        result = asyncio.run(executor.execute({"task": "analyze", "mode": "quick"}))
        assert result["result"] == "quick result"

    def test_missing_required_input(self, simple_manifest: ExecutionManifest, tmp_skill: Path) -> None:
        executor = _make_executor(simple_manifest, tmp_skill)
        with pytest.raises(ValueError, match="Required input missing"):
            asyncio.run(executor.execute({}))

    def test_input_coercion(self, tmp_skill: Path) -> None:
        manifest = ExecutionManifest(
            input_schema={"count": {"type": "integer", "required": True}},
            steps=[
                {"id": "s", "type": "prompt", "prompt": "prompts/plan.md", "output": "r"},
            ],
            output={"primary": "r"},
        )
        executor = _make_executor(manifest, tmp_skill)
        result = asyncio.run(executor.execute({"count": "42"}))
        assert result["result"] == "mock response"
