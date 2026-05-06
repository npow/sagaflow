"""Temporal integration for manifest-driven skills.

ManifestWorkflow is a single registered Temporal workflow class that
handles all manifested skills. The skill slug in ManifestInput selects
which SKILL.md to load.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from temporalio import activity, workflow
from temporalio.common import RetryPolicy as TemporalRetryPolicy

from sagaflow.manifest.schema import ExecutionManifest, SkillFrontmatter

_DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _skills_dir() -> Path:
    return Path(os.environ.get("CLAUDE_SKILLS_DIR", str(_DEFAULT_SKILLS_DIR)))


def find_skill_root(slug: str) -> Path:
    candidates = [
        _skills_dir() / slug,
    ]
    for path in candidates:
        if (path / "SKILL.md").exists():
            return path
    raise FileNotFoundError(f"Skill not found: {slug!r}")


def load_manifest(skill_root: Path) -> ExecutionManifest:
    text = (skill_root / "SKILL.md").read_text()
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"No YAML frontmatter found in {skill_root}/SKILL.md")
    fm = yaml.safe_load(parts[1])
    parsed = SkillFrontmatter(**fm)
    if parsed.execution is None:
        raise ValueError(f"No 'execution' key in {skill_root}/SKILL.md frontmatter")
    return parsed.execution


@dataclass
class ManifestInput:
    """Generic input for all manifested skills."""
    skill_slug: str
    inputs: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""
    run_id: str = ""
    inbox_path: str = ""
    notify: bool = True


@dataclass
class LLMCallInput:
    system: str
    prompt: str
    model: str
    timeout_seconds: int = 300


@activity.defn(name="manifest_llm_call")
async def llm_call_activity(inp: LLMCallInput) -> str:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    kwargs: dict[str, Any] = {
        "model": inp.model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": inp.prompt}],
    }
    if inp.system:
        kwargs["system"] = inp.system
    response = await client.messages.create(**kwargs)
    return response.content[0].text


@activity.defn(name="manifest_resolve_skill_root")
async def resolve_skill_root_activity(slug: str) -> str:
    return str(find_skill_root(slug))


_DEFAULT_RETRY = TemporalRetryPolicy(maximum_attempts=3)


@workflow.defn(name="ManifestWorkflow")
class ManifestWorkflow:

    @workflow.run
    async def run(self, inp: ManifestInput) -> dict[str, Any]:
        from sagaflow.manifest.executor import ManifestExecutor

        skill_root_str = await workflow.execute_activity(
            "manifest_resolve_skill_root",
            inp.skill_slug,
            start_to_close_timeout=timedelta(seconds=10),
        )
        skill_root = Path(skill_root_str)
        manifest = load_manifest(skill_root)

        async def model_call(system: str, prompt: str, opts: dict[str, Any]) -> str:
            timeout = opts.get("timeout_seconds", 300)
            return await workflow.execute_activity(
                "manifest_llm_call",
                LLMCallInput(
                    system=system,
                    prompt=prompt,
                    model=opts.get("model", "claude-sonnet-4-6"),
                    timeout_seconds=timeout,
                ),
                start_to_close_timeout=timedelta(seconds=timeout + 30),
                retry_policy=_DEFAULT_RETRY,
            )

        async def dispatch_call(skill: str, inputs: dict[str, Any]) -> Any:
            return await workflow.execute_child_workflow(
                ManifestWorkflow.run,
                ManifestInput(skill_slug=skill, inputs=inputs),
                id=f"{skill}-child-{workflow.info().workflow_id}",
                execution_timeout=timedelta(seconds=1800),
            )

        executor = ManifestExecutor(
            manifest=manifest,
            skill_root=skill_root,
            model_call=model_call,
            dispatch_call=dispatch_call,
        )
        return await executor.execute(inp.inputs)
