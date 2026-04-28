"""ChainWorkflow — orchestrates multi-skill pipelines from _chains/ declarations.

Executes skills in declared order, threads file manifests between steps,
and handles error propagation per the declared on_failure policy.

No new Temporal primitives: delegates execution to ClaudeSkillWorkflow
as child workflows.  Chain declaration is loaded once at workflow start
for replay safety.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from sagaflow.temporal_client import TASK_QUEUE


# ---------------------------------------------------------------------------
# Error types (§2.5.3)
# ---------------------------------------------------------------------------


class ChainValidationError(Exception):
    """Raised during validation or lazy path resolution.  Chain did not
    start or step did not execute."""


class ChainAbortError(Exception):
    """Raised when on_failure=abort is triggered during execution."""

    def __init__(self, step_id: str, termination_label: str) -> None:
        self.step_id = step_id
        self.termination_label = termination_label
        super().__init__(f"chain aborted at step {step_id!r}: {termination_label}")


# ---------------------------------------------------------------------------
# Workflow input (§2.5.1)
# ---------------------------------------------------------------------------


@dataclass
class ChainWorkflowInput:
    chain_id: str
    chain_version: int
    run_dir: str
    chain_input: dict = field(default_factory=dict)
    initiated_by: str = "operator"
    dry_run: bool = False
    inbox_path: str = ""
    skills_dir: str = ""
    chains_dir: str = ""


# ---------------------------------------------------------------------------
# Termination labels
# ---------------------------------------------------------------------------

VALID_TERMINATION_LABELS = frozenset({"COMPLETED", "PARTIAL", "FAILED", "TIMED_OUT"})

# CHAIN_STATUS step-level statuses
STEP_PENDING = "PENDING"
STEP_RUNNING = "RUNNING"
STEP_COMPLETED = "COMPLETED"
STEP_SKIPPED_PARTIAL = "SKIPPED_PARTIAL"
STEP_SKIPPED_FAILED = "SKIPPED_FAILED"
STEP_FAILED = "FAILED"

# Retry backoff
_RETRY_BASE_SECONDS = 30
_RETRY_MAX_SECONDS = 300


# ---------------------------------------------------------------------------
# Activity inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadChainDeclarationInput:
    chains_dir: str
    chain_id: str


@dataclass(frozen=True)
class ResolveSkillContentInput:
    skills_dir: str
    skill_name: str


@dataclass(frozen=True)
class WriteChainFileInput:
    path: str
    content: str


@dataclass(frozen=True)
class ReadStepTerminationInput:
    done_path: str


@dataclass(frozen=True)
class CheckExportsInput:
    run_dir: str
    step_id: str
    export_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@activity.defn(name="load_chain_declaration")
async def load_chain_declaration(inp: LoadChainDeclarationInput) -> dict:
    chains_dir = Path(inp.chains_dir)
    chain_file = chains_dir / f"{inp.chain_id}.json"
    if not chain_file.exists():
        raise ChainValidationError(
            f"chain declaration not found: {chain_file}"
        )
    text = chain_file.read_text(encoding="utf-8")
    try:
        decl = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChainValidationError(
            f"invalid JSON in {chain_file}: {exc}"
        ) from exc
    if not isinstance(decl, dict):
        raise ChainValidationError(
            f"chain declaration must be a JSON object, got {type(decl).__name__}"
        )
    return decl


@activity.defn(name="resolve_skill_content")
async def resolve_skill_content(inp: ResolveSkillContentInput) -> str:
    skills_dir = Path(inp.skills_dir)
    # Try bare name first, then with -temporal suffix
    for candidate in (inp.skill_name, f"{inp.skill_name}-temporal"):
        skill_md = skills_dir / candidate / "SKILL.md"
        if skill_md.exists():
            return skill_md.read_text(encoding="utf-8")
    raise ChainValidationError(
        f"SKILL.md not found for {inp.skill_name!r} "
        f"(searched {skills_dir / inp.skill_name} and "
        f"{skills_dir / (inp.skill_name + '-temporal')})"
    )


@activity.defn(name="write_chain_file")
async def write_chain_file(inp: WriteChainFileInput) -> None:
    target = Path(inp.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(inp.content, encoding="utf-8")
    fd = os.open(str(target), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@activity.defn(name="read_step_termination")
async def read_step_termination(inp: ReadStepTerminationInput) -> str:
    done_path = Path(inp.done_path)
    if not done_path.exists():
        return "FAILED"
    text = done_path.read_text(encoding="utf-8").strip()
    if not text:
        return "FAILED"
    first_line = text.splitlines()[0].strip()
    if first_line in VALID_TERMINATION_LABELS:
        return first_line
    activity.logger.warning(
        "Malformed termination label in %s: %r — treating as FAILED",
        done_path, first_line,
    )
    return "FAILED"


@activity.defn(name="check_chain_exports")
async def check_chain_exports(inp: CheckExportsInput) -> dict:
    result: dict[str, str | None] = {}
    step_dir = Path(inp.run_dir) / inp.step_id
    for rel_path in inp.export_paths:
        full = step_dir / rel_path
        if full.exists() and full.stat().st_size > 0:
            result[rel_path] = str(full)
        else:
            result[rel_path] = None
    return result


# ---------------------------------------------------------------------------
# Helpers (workflow-safe: no I/O)
# ---------------------------------------------------------------------------


def _resolve_jsonpath(path_expr: str, context: dict) -> str:
    """Resolve a JSONPath-style expression against the chain context.

    Supports:
      $.chain_input.<key>
      $.steps.<step_id>.outputs.<key>
      $.steps.<step_id>.termination
      $.steps.<step_id>.run_dir
    """
    if not path_expr.startswith("$."):
        raise ChainValidationError(f"invalid JSONPath expression: {path_expr!r}")

    parts = path_expr[2:].split(".")
    obj: object = context
    for part in parts:
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            raise ChainValidationError(
                f"cannot resolve {path_expr!r}: key {part!r} not found"
            )
    if not isinstance(obj, str):
        return json.dumps(obj) if not isinstance(obj, (int, float, bool)) else str(obj)
    return obj


def _parse_on_failure(value: str) -> tuple[str, int]:
    """Parse on_failure field: 'abort', 'skip', or 'retry(n)'.

    Returns (policy, retries) where policy is 'abort'|'skip'|'retry'
    and retries is 0 for abort/skip, n for retry(n).
    """
    if value == "abort":
        return ("abort", 0)
    if value == "skip":
        return ("skip", 0)
    m = re.match(r"^retry\((\d+)\)$", value)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 3:
            raise ChainValidationError(
                f"retry count must be 1-3, got {n}"
            )
        return ("retry", n)
    raise ChainValidationError(f"invalid on_failure value: {value!r}")


def _backoff_seconds(attempt: int) -> float:
    delay = _RETRY_BASE_SECONDS * (2 ** attempt)
    return min(delay, _RETRY_MAX_SECONDS)


# ---------------------------------------------------------------------------
# ChainWorkflow (§2.5.2)
# ---------------------------------------------------------------------------


@workflow.defn(name="ChainWorkflow")
class ChainWorkflow:
    """Orchestrates a declared skill chain from _chains/<chain_id>.json."""

    def __init__(self) -> None:
        self._chain_status: dict = {}

    @workflow.run
    async def run(self, inp: ChainWorkflowInput) -> str:
        # Step 1: Load chain declaration once (replay-safe).
        decl = await workflow.execute_activity(
            load_chain_declaration,
            LoadChainDeclarationInput(
                chains_dir=inp.chains_dir,
                chain_id=inp.chain_id,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )

        # Step 2: Validate.
        self._validate_declaration(decl, inp)

        steps = decl["steps"]

        # Step 3: Dry run — validate and return without executing.
        if inp.dry_run:
            return json.dumps({"dry_run": True, "chain_id": inp.chain_id, "steps": len(steps)})

        # Step 4: Initialize CHAIN_STATUS.json.
        wf_id = workflow.info().workflow_id
        self._chain_status = {
            "chain_id": inp.chain_id,
            "chain_run_id": wf_id,
            "status": "RUNNING",
            "steps": {s["step_id"]: STEP_PENDING for s in steps},
            "failed_step": None,
            "failure_reason": None,
        }
        await self._write_status(inp.run_dir)

        # Chain context accumulates resolved outputs from completed steps.
        chain_context: dict = {
            "chain_input": inp.chain_input,
            "steps": {},
        }

        # Step 5: Execute each step in order.
        try:
            for step_def in steps:
                await self._execute_step(step_def, inp, chain_context)
        except ChainAbortError as exc:
            # Step 7: Handle abort.
            self._chain_status["status"] = "FAILED"
            self._chain_status["failed_step"] = exc.step_id
            self._chain_status["failure_reason"] = exc.termination_label
            await self._write_status(inp.run_dir)

            on_chain_failure = decl.get("on_chain_failure", "leave_artifacts")
            if on_chain_failure == "clean_artifacts":
                pass  # preserve status + inputs, would delete step outputs

            raise

        # Step 6: All steps completed.
        self._chain_status["status"] = "COMPLETED"
        await self._write_status(inp.run_dir)
        return json.dumps({
            "chain_id": inp.chain_id,
            "status": "COMPLETED",
            "steps_completed": len(steps),
        })

    def _validate_declaration(self, decl: dict, inp: ChainWorkflowInput) -> None:
        """Step 2: Validate the chain declaration before any execution."""
        if decl.get("version") != inp.chain_version:
            raise ChainValidationError(
                f"version mismatch: declaration has {decl.get('version')}, "
                f"input expects {inp.chain_version}"
            )

        steps = decl.get("steps")
        if not steps or not isinstance(steps, list):
            raise ChainValidationError("chain must have at least one step")

        step_ids = set()
        for step in steps:
            sid = step.get("step_id")
            if not sid:
                raise ChainValidationError("step missing step_id")
            if sid in step_ids:
                raise ChainValidationError(f"duplicate step_id: {sid!r}")
            step_ids.add(sid)

            if not step.get("skill"):
                raise ChainValidationError(f"step {sid!r} missing skill")

            _parse_on_failure(step.get("on_failure", "abort"))

            # Validate $.chain_input.* paths eagerly.
            for key, path_expr in step.get("input_mapping", {}).items():
                if path_expr.startswith("$.chain_input."):
                    try:
                        _resolve_jsonpath(path_expr, {"chain_input": inp.chain_input, "steps": {}})
                    except ChainValidationError:
                        raise ChainValidationError(
                            f"step {sid!r}, input {key!r}: "
                            f"cannot resolve {path_expr!r} against chain_input"
                        )

    async def _execute_step(
        self,
        step_def: dict,
        inp: ChainWorkflowInput,
        chain_context: dict,
    ) -> None:
        """Execute a single chain step with retry/skip/abort policy."""
        step_id = step_def["step_id"]
        skill = step_def["skill"]
        policy, max_retries = _parse_on_failure(step_def.get("on_failure", "abort"))

        attempts = max_retries + 1 if policy == "retry" else 1

        for attempt in range(attempts):
            if attempt > 0:
                delay = _backoff_seconds(attempt - 1)
                await workflow.sleep(delay)

            # 5a: Resolve input_mapping.
            resolved_inputs = self._resolve_inputs(step_def, chain_context)

            step_run_dir = str(Path(inp.run_dir) / step_id)
            inputs_dir = str(Path(step_run_dir) / "inputs")

            # 5b: Write resolved inputs as files.
            for key, value in resolved_inputs.items():
                await workflow.execute_activity(
                    write_chain_file,
                    WriteChainFileInput(
                        path=str(Path(inputs_dir) / f"{key}.txt"),
                        content=value if isinstance(value, str) else json.dumps(value),
                    ),
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                )

            # 5c: Write CHAIN_MANIFEST.json.
            manifest = {
                "chain_id": inp.chain_id,
                "chain_run_id": workflow.info().workflow_id,
                "step_id": step_id,
                "step_index": next(
                    i for i, s in enumerate(self._chain_status["steps"])
                    if s == step_id
                ),
                "total_steps": len(self._chain_status["steps"]),
                "inputs": resolved_inputs,
                "upstream_terminations": {
                    sid: chain_context["steps"][sid].get("termination", "UNKNOWN")
                    for sid in chain_context["steps"]
                },
            }
            await workflow.execute_activity(
                write_chain_file,
                WriteChainFileInput(
                    path=str(Path(inputs_dir) / "CHAIN_MANIFEST.json"),
                    content=json.dumps(manifest, indent=2),
                ),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )

            # 5d: Update status to RUNNING.
            self._chain_status["steps"][step_id] = STEP_RUNNING
            await self._write_status(inp.run_dir)

            # 5e: Execute ClaudeSkillWorkflow as child workflow.
            skill_content = await workflow.execute_activity(
                resolve_skill_content,
                ResolveSkillContentInput(
                    skills_dir=inp.skills_dir,
                    skill_name=skill,
                ),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )

            # Build user_args from resolved inputs.
            user_args = {k: v for k, v in resolved_inputs.items() if isinstance(v, str)}

            with workflow.unsafe.imports_passed_through():
                from sagaflow.generic.workflow import ClaudeSkillInput, ClaudeSkillWorkflow

            run_id = f"{workflow.info().workflow_id}-{step_id}"
            child_input = ClaudeSkillInput(
                run_id=run_id,
                run_dir=step_run_dir,
                inbox_path=inp.inbox_path,
                skill_name=skill,
                skill_md_content=skill_content,
                user_args=user_args,
            )

            await workflow.execute_child_workflow(
                ClaudeSkillWorkflow.run,
                child_input,
                id=run_id,
                task_queue=TASK_QUEUE,
            )

            # 5f: Read termination label.
            done_path = str(Path(step_run_dir) / "DONE")
            label = await workflow.execute_activity(
                read_step_termination,
                ReadStepTerminationInput(done_path=done_path),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
            )

            # 5g: Evaluate on_failure policy.
            if label == "COMPLETED":
                await self._register_exports(step_def, step_run_dir, chain_context)
                self._chain_status["steps"][step_id] = STEP_COMPLETED
                await self._write_status(inp.run_dir)
                return

            if label == "PARTIAL":
                if policy == "skip":
                    await self._register_exports(step_def, step_run_dir, chain_context)
                    chain_context["steps"].setdefault(step_id, {})["termination"] = label
                    self._chain_status["steps"][step_id] = STEP_SKIPPED_PARTIAL
                    await self._write_status(inp.run_dir)
                    return
                if policy == "retry" and attempt < attempts - 1:
                    continue
                # abort (or retries exhausted)
                self._chain_status["steps"][step_id] = STEP_FAILED
                await self._write_status(inp.run_dir)
                raise ChainAbortError(step_id=step_id, termination_label=label)

            # FAILED or TIMED_OUT
            if policy == "skip":
                chain_context["steps"].setdefault(step_id, {})["termination"] = label
                self._chain_status["steps"][step_id] = STEP_SKIPPED_FAILED
                await self._write_status(inp.run_dir)
                return
            if policy == "retry" and attempt < attempts - 1:
                continue
            # abort (or retries exhausted)
            self._chain_status["steps"][step_id] = STEP_FAILED
            await self._write_status(inp.run_dir)
            raise ChainAbortError(step_id=step_id, termination_label=label)

    def _resolve_inputs(self, step_def: dict, chain_context: dict) -> dict:
        """Step 5a: Resolve all input_mapping paths for a step."""
        resolved = {}
        for key, path_expr in step_def.get("input_mapping", {}).items():
            resolved[key] = _resolve_jsonpath(path_expr, chain_context)
        return resolved

    async def _register_exports(
        self,
        step_def: dict,
        step_run_dir: str,
        chain_context: dict,
    ) -> dict:
        """Register output exports into chain context for downstream steps."""
        step_id = step_def["step_id"]
        exports = step_def.get("output_exports", [])
        export_paths = [e["path"] for e in exports]

        result = await workflow.execute_activity(
            check_chain_exports,
            CheckExportsInput(
                run_dir=str(Path(step_run_dir).parent),
                step_id=step_id,
                export_paths=export_paths,
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )

        step_ctx: dict = chain_context.setdefault("steps", {}).setdefault(step_id, {})
        step_ctx["run_dir"] = step_run_dir
        step_ctx["termination"] = "COMPLETED"
        outputs: dict = {}
        for export_def in exports:
            key = export_def["key"]
            rel_path = export_def["path"]
            resolved = result.get(rel_path)
            if resolved:
                outputs[key] = resolved
            else:
                workflow.logger.warning(
                    "Export %s/%s not found at %s — skipping",
                    step_id, key, rel_path,
                )
        step_ctx["outputs"] = outputs
        return outputs

    async def _write_status(self, run_dir: str) -> None:
        await workflow.execute_activity(
            write_chain_file,
            WriteChainFileInput(
                path=str(Path(run_dir) / "CHAIN_STATUS.json"),
                content=json.dumps(self._chain_status, indent=2),
            ),
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
