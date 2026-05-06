"""Manifest-driven skill execution engine.

Skills declare their orchestration in SKILL.md YAML frontmatter.
Both Temporal (ManifestWorkflow) and in-session (ManifestInterpreter)
consume the same manifest through a shared ManifestExecutor engine.
"""

from sagaflow.manifest.schema import ExecutionManifest, SkillFrontmatter
from sagaflow.manifest.executor import ManifestExecutor
from sagaflow.manifest.context import ExecutionContext
from sagaflow.manifest.prompts import PromptRegistry

__all__ = [
    "ExecutionManifest",
    "ExecutionContext",
    "ManifestExecutor",
    "PromptRegistry",
    "SkillFrontmatter",
]
