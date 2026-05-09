"""Unit tests for the @skill decorator in sagaflow.simple.

Verifies SkillSpec wiring (activities, CLI options, build_input). The
actual Temporal end-to-end execution path uses the same activities as
the existing hello-world skill (whose e2e test lives in tests/skills/
and is excluded from CI), so this file stays unit-level for fast CI.
"""

from __future__ import annotations

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
    activity_names = {
        getattr(a, "__temporal_activity_definition", None).name
        for a in hello.spec.activities
        if getattr(a, "__temporal_activity_definition", None)
    }
    assert {"write_artifact", "emit_finding", "spawn_subagent"}.issubset(activity_names)
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


def test_required_param_without_default_raises_when_omitted() -> None:
    @skill("needs-name")
    async def needs_name(ctx, name: str) -> str:  # no default
        return name

    with pytest.raises(TypeError, match="requires --name"):
        needs_name.spec.build_input(
            run_id="r3", run_dir="/tmp/r3", inbox_path="/tmp/INBOX.md", cli_args={},
        )
