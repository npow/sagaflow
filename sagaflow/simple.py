"""Simplified skill-authoring layer over the @workflow.defn / SkillSpec API.

The 80% case for sagaflow is "load a prompt, ask a model, return the result."
The full Temporal-style API (workflow class + execute_activity + retry policies
+ Input dataclass + SkillSpec wiring) is the right escape hatch for genuine
multi-step orchestration, but it's heavy for trivial skills.

This module provides ``@skill`` and a ``ctx.prompt(...)`` helper so that
hello-world is six lines:

    from sagaflow import skill

    @skill("hello-world")
    async def hello(ctx, name: str = "world") -> str:
        return await ctx.prompt("greeter", name=name)

The decorator builds the Input dataclass from the function signature, wraps
the body in a Temporal ``@workflow.defn`` class, registers the standard
activities, and auto-emits the return value to the inbox. ``ctx.prompt(...)``
collapses the write_artifact -> spawn_subagent -> parse-STRUCTURED_OUTPUT
chain into one durable call.

The generated SkillSpec is attached to the decorated function as
``func.spec``; user code wires it in by writing ``register = hello.register``
in the skill's ``__init__.py``.
"""

from __future__ import annotations

import inspect
from dataclasses import field, make_dataclass
from datetime import timedelta
from pathlib import Path
from string import Template
from typing import Any, Callable

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.durable.activities import (
        EmitFindingInput,
        SpawnSubagentInput,
        WriteArtifactInput,
        emit_finding,
        spawn_subagent,
        write_artifact,
    )
    from sagaflow.durable.retry_policies import HAIKU_POLICY
    from sagaflow.registry import SkillRegistry, SkillSpec


_DEFAULT_TIER = "HAIKU"


class SkillContext:
    """Runtime context passed to a ``@skill``-decorated function.

    Held by the workflow during execution. ``prompt`` is the durable
    LLM-call helper; ``run_id``, ``run_dir``, ``inbox_path`` are surfaced
    for advanced cases (custom artifacts, side files).
    """

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: str,
        inbox_path: str,
        prompts_dir: Path,
        skill_name: str,
    ) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.inbox_path = inbox_path
        self._prompts_dir = prompts_dir
        self._skill_name = skill_name

    async def prompt(
        self,
        role: str,
        *,
        tier: str = _DEFAULT_TIER,
        max_tokens: int = 1024,
        **vars: Any,
    ) -> Any:
        """Run a sub-agent prompt durably and return its parsed output.

        Loads ``prompts/<role>.system.md`` and ``prompts/<role>.user.md``
        from the skill directory, substitutes ``$var`` placeholders, writes
        the rendered user prompt to the run dir as a checkpoint, then spawns
        a sub-agent (HAIKU/SONNET/OPUS) and parses its STRUCTURED_OUTPUT
        block. If the result has a single key, returns that value as-is;
        otherwise returns the dict.
        """
        sys_path = self._prompts_dir / f"{role}.system.md"
        user_path = self._prompts_dir / f"{role}.user.md"
        sys_text = sys_path.read_text()
        user_template = user_path.read_text()
        user_text = Template(user_template).substitute({k: str(v) for k, v in vars.items()})

        prompt_artifact = f"{self.run_dir}/{role}.user.txt"
        await workflow.execute_activity(
            "write_artifact",
            WriteArtifactInput(path=prompt_artifact, content=user_text),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=HAIKU_POLICY,
        )
        parsed = await workflow.execute_activity(
            "spawn_subagent",
            SpawnSubagentInput(
                role=role,
                tier_name=tier,
                system_prompt=sys_text,
                user_prompt_path=prompt_artifact,
                max_tokens=max_tokens,
                tools_needed=False,
            ),
            start_to_close_timeout=timedelta(seconds=600),
            heartbeat_timeout=timedelta(seconds=120),
            retry_policy=HAIKU_POLICY,
        )
        if isinstance(parsed, dict) and len(parsed) == 1:
            return next(iter(parsed.values()))
        return parsed


def skill(
    name: str,
    *,
    description: str | None = None,
    notify: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate an async function to register it as a sagaflow skill.

    The function must take ``ctx`` (a :class:`SkillContext`) as its first
    parameter; remaining parameters become CLI flags + workflow input fields.
    The return value is converted to a string and emitted to the inbox.

    Pair with a one-line ``register = func.register`` in the skill's
    ``__init__.py`` so the worker discovers it.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        if not params or params[0].name != "ctx":
            raise TypeError(
                f"@skill function must take 'ctx' as its first parameter; "
                f"got {[p.name for p in params]}"
            )
        user_params = params[1:]

        cli_options: list[tuple[str, dict[str, Any]]] = []
        for p in user_params:
            opt: dict[str, Any] = {}
            if p.default is not inspect.Parameter.empty:
                opt["default"] = p.default
            else:
                opt["required"] = True
            if p.annotation in (str, int, float, bool):
                opt["type"] = p.annotation
            if description:
                opt["help"] = description
            cli_options.append((p.name, opt))

        fields_spec: list[Any] = [
            ("run_id", str),
            ("run_dir", str),
            ("inbox_path", str),
        ]
        for p in user_params:
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else Any
            if p.default is not inspect.Parameter.empty:
                fields_spec.append((p.name, ann, field(default=p.default)))
            else:
                fields_spec.append((p.name, ann))

        safe_name = name.replace("-", "_")
        InputCls = make_dataclass(f"_{safe_name}_Input", fields_spec, frozen=True)
        prompts_dir = Path(inspect.getfile(func)).resolve().parent / "prompts"
        skill_name = name
        cls_name = f"_SagaflowSkillWorkflow_{safe_name}"

        async def _run(self, inp):  # type: ignore[no-untyped-def]
            # The Any-typed signature above is purely a placeholder; Temporal
            # introspects __annotations__ (set below after class construction)
            # to know the actual InputCls type for payload deserialization.
            ctx = SkillContext(
                run_id=inp.run_id,
                run_dir=inp.run_dir,
                inbox_path=inp.inbox_path,
                prompts_dir=prompts_dir,
                skill_name=skill_name,
            )
            user_kwargs = {p.name: getattr(inp, p.name) for p in user_params}
            result = await func(ctx, **user_kwargs)

            summary = result if isinstance(result, str) else str(result)
            await workflow.execute_activity(
                "emit_finding",
                EmitFindingInput(
                    inbox_path=inp.inbox_path,
                    run_id=inp.run_id,
                    skill=skill_name,
                    status="DONE",
                    summary=summary,
                    notify=notify,
                    timestamp_iso=workflow.now().isoformat(timespec="seconds"),
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=HAIKU_POLICY,
            )
            return result

        # Stable qualname/module so Temporal accepts the class.
        user_module = inspect.getmodule(func)
        module_name = user_module.__name__ if user_module is not None else func.__module__
        _run.__module__ = module_name
        _run.__qualname__ = f"{cls_name}.run"
        _run.__name__ = "run"
        # Type annotation is load-bearing: Temporal's payload converter reads
        # __annotations__["inp"] to deserialize the workflow input back into
        # the InputCls dataclass. Without this, the workflow receives a dict.
        _run.__annotations__ = {"inp": InputCls, "return": Any}
        decorated_run = workflow.run(_run)

        _Workflow = type(cls_name, (), {"run": decorated_run})
        _Workflow.__module__ = module_name
        _Workflow.__qualname__ = cls_name
        if user_module is not None:
            setattr(user_module, cls_name, _Workflow)
        _Workflow = workflow.defn(name=f"_skill_{skill_name}")(_Workflow)

        def _build_input(
            *, run_id: str, run_dir: str, inbox_path: str, cli_args: dict[str, Any]
        ) -> Any:
            user_kwargs: dict[str, Any] = {}
            for p in user_params:
                if p.name in cli_args:
                    user_kwargs[p.name] = cli_args[p.name]
                elif p.default is not inspect.Parameter.empty:
                    user_kwargs[p.name] = p.default
                else:
                    raise TypeError(
                        f"skill {skill_name!r} requires --{p.name} (no default)"
                    )
            return InputCls(
                run_id=run_id, run_dir=run_dir, inbox_path=inbox_path, **user_kwargs
            )

        spec = SkillSpec(
            name=skill_name,
            workflow_cls=_Workflow,
            activities=[write_artifact, emit_finding, spawn_subagent],
            build_input=_build_input,
            cli_options=cli_options,
        )

        def register_with(registry: SkillRegistry) -> None:
            registry.register(spec)

        func.spec = spec  # type: ignore[attr-defined]
        func.register = register_with  # type: ignore[attr-defined]
        return func

    return decorator
