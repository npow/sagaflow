"""Unit tests for the @skill decorator in sagaflow.simple.

Verifies SkillSpec wiring (activities, CLI options, build_input). The
actual Temporal end-to-end execution path uses the same activities as
the existing hello-world skill (whose e2e test lives in tests/skills/
and is excluded from CI), so this file stays unit-level for fast CI.
"""

from __future__ import annotations

import os

import pytest

from sagaflow import skill
from sagaflow.registry import SkillRegistry


@skill("hello-simple")
async def hello(ctx, name: str = "world") -> str:
    return await ctx.prompt("greeter", name=name)


def test_decorator_attaches_spec_and_register_helper() -> None:
    assert hasattr(hello, "spec"), "decorator should attach .spec"
    assert hasattr(hello, "register"), "decorator should attach .register"
    assert hello.spec.name == "hello-simple"
    activity_func_names = {a.__name__ for a in hello.spec.activities}
    assert {"write_artifact", "emit_finding", "spawn_subagent"}.issubset(activity_func_names)
    opt_names = [name for name, _ in hello.spec.cli_options]
    assert opt_names == ["name"]
    assert hello.spec.cli_options[0][1]["default"] == "world"


def test_register_with_registry() -> None:
    registry = SkillRegistry()
    hello.register(registry)
    assert "hello-simple" in list(registry.names())


def test_build_input_uses_cli_args() -> None:
    inp = hello.spec.build_input(
        run_id="r1", run_dir="/tmp/r1", inbox_path="/tmp/INBOX.md",
        cli_args={"name": "alice"},
    )
    assert inp.name == "alice"
    assert inp.run_id == "r1"


def test_build_input_falls_back_to_defaults() -> None:
    inp = hello.spec.build_input(
        run_id="r2", run_dir="/tmp/r2", inbox_path="/tmp/INBOX.md", cli_args={},
    )
    assert inp.name == "world"


def test_decorator_rejects_function_without_ctx_first_param() -> None:
    with pytest.raises(TypeError, match="must take 'ctx'"):
        @skill("bad-skill")
        async def bad(name: str) -> str:  # missing ctx
            return name


def test_workflow_run_has_input_class_annotation() -> None:
    """Regression guard: Temporal's payload converter introspects
    ``run.__annotations__["inp"]`` to deserialize the workflow input. A
    missing or wrong annotation causes the workflow to receive a raw dict
    instead of the InputCls instance, surfacing as
    ``AttributeError: 'dict' object has no attribute 'run_id'`` at runtime
    inside the worker — invisible to structural-only unit tests."""
    run_method = hello.spec.workflow_cls.run
    annotations = run_method.__annotations__
    assert "inp" in annotations, "run method must annotate its 'inp' parameter for Temporal"
    inp_type = annotations["inp"]
    # The input class is a dataclass synthesized from the skill's signature.
    assert hasattr(inp_type, "__dataclass_fields__"), f"inp annotation must be a dataclass, got {inp_type!r}"
    # Must include the framework-injected fields plus user params.
    field_names = set(inp_type.__dataclass_fields__.keys())
    assert {"run_id", "run_dir", "inbox_path", "name"}.issubset(field_names)


@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason=(
        "tests/conftest.py replaces temporalio with a _Noop stub in CI to keep "
        "tokio threads from being killed mid-job. The stubbed payload_converter "
        "can't actually round-trip data — see the real-temporalio variant in "
        "the e2e suite (tests/skills/, excluded from CI)."
    ),
)
def test_input_class_roundtrips_through_temporal_payload_converter() -> None:
    """Catches the same class of failure as above but at one layer deeper:
    even if the annotation is set, Temporal must be able to serialize the
    InputCls instance, then deserialize it back into the same type. Uses
    Temporal's default converter directly — no server, ~milliseconds."""
    from temporalio.converter import default

    converter = default()
    inp = hello.spec.build_input(
        run_id="r1", run_dir="/tmp/r1", inbox_path="/tmp/INBOX.md",
        cli_args={"name": "alice"},
    )
    payloads = converter.payload_converter.to_payloads([inp])
    inp_type = hello.spec.workflow_cls.run.__annotations__["inp"]
    decoded = converter.payload_converter.from_payloads(payloads, [inp_type])
    assert decoded[0].name == "alice"
    assert decoded[0].run_id == "r1"
    assert isinstance(decoded[0], inp_type), f"decoded type {type(decoded[0])!r} != expected {inp_type!r}"


def test_required_param_without_default_raises_when_omitted() -> None:
    @skill("needs-name")
    async def needs_name(ctx, name: str) -> str:  # no default
        return name

    with pytest.raises(TypeError, match="requires --name"):
        needs_name.spec.build_input(
            run_id="r3", run_dir="/tmp/r3", inbox_path="/tmp/INBOX.md", cli_args={},
        )
