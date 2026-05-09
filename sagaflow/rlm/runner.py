"""RLM research runner — executes a research query using DSPy RLM sandbox.

The LLM writes Python code to explore research data through tools. Tool results
stay in the sandbox (never entering the LLM's context). Sub-LLM calls use Haiku.

Usage as CLI:
    python -m sagaflow.rlm.runner --query "How does Temporal work at Netflix?" \
        --run-dir /tmp/rlm-test --verbose

Usage as library:
    from sagaflow.rlm.runner import run_research
    result = run_research("How does Temporal work at Netflix?", run_dir="/tmp/rlm-test")
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MGP_BASE = os.environ.get(
    "MGP_BASE_URL", "http://mgp.local.dev.netflix.net:9123/proxy/npowws/v1"
)
MGP_KEY = os.environ.get("MGP_API_KEY", "sk-dummy")

MAIN_MODEL = os.environ.get("RLM_MAIN_MODEL", "openai/claude-sonnet-4-6")
SUB_MODEL = os.environ.get("RLM_SUB_MODEL", "openai/claude-haiku-4-5")

DENO_PATH = os.environ.get("DENO_PATH", os.path.expanduser("~/.deno/bin/deno"))


@dataclass
class RlmResult:
    query: str
    findings: str
    trajectory: list[dict] = field(default_factory=list)
    iterations: int = 0
    elapsed_seconds: float = 0.0
    main_model: str = ""
    sub_model: str = ""
    error: str | None = None


def _ensure_deno_on_path() -> None:
    deno_dir = os.path.dirname(DENO_PATH)
    if deno_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{deno_dir}:{os.environ.get('PATH', '')}"


def run_research(
    query: str,
    *,
    run_dir: str = "/tmp/rlm-research",
    max_iterations: int = 15,
    max_llm_calls: int = 30,
    verbose: bool = False,
    tools: list | None = None,
    main_model: str | None = None,
    sub_model: str | None = None,
) -> RlmResult:
    """Execute a research query using DSPy RLM with sandboxed code execution.

    Args:
        query: The research question.
        run_dir: Directory for output artifacts.
        max_iterations: Max RLM code-execute iterations.
        max_llm_calls: Max sub-LLM (Haiku) calls within sandbox.
        verbose: Enable detailed logging.
        tools: Override default research tools.
        main_model: Override main LM model ID.
        sub_model: Override sub LM model ID.

    Returns:
        RlmResult with findings and execution metadata.
    """
    import dspy

    from sagaflow.rlm.tools import ALL_TOOLS

    _ensure_deno_on_path()

    main_m = main_model or MAIN_MODEL
    sub_m = sub_model or SUB_MODEL

    main_lm = dspy.LM(main_m, api_base=MGP_BASE, api_key=MGP_KEY)
    sub_lm = dspy.LM(sub_m, api_base=MGP_BASE, api_key=MGP_KEY)
    dspy.configure(lm=main_lm)

    research_tools = tools if tools is not None else ALL_TOOLS

    system_instructions = (
        "You are a research agent with access to Netflix's codebase (search_codebase), "
        "internal documentation (search_docs), Slack history (search_slack), and local "
        "files (read_file). You also have llm_query() for semantic analysis.\n\n"
        "APPROACH:\n"
        "1. Break the research query into sub-questions\n"
        "2. Use the search tools to gather relevant data\n"
        "3. Use llm_query() with Haiku for extracting specific facts from large results\n"
        "4. Synthesize findings across sources\n"
        "5. SUBMIT your findings as a structured markdown report\n\n"
        "IMPORTANT:\n"
        "- Each tool returns a string. Store results in variables and process them.\n"
        "- Use llm_query(prompt) for any semantic extraction — it's cheap (Haiku).\n"
        "- Be thorough: search multiple sources, cross-reference findings.\n"
        "- SUBMIT when you have a comprehensive answer, don't over-iterate.\n"
    )

    rlm = dspy.RLM(
        f"query: str -> findings: str",
        max_iterations=max_iterations,
        max_llm_calls=max_llm_calls,
        verbose=verbose,
        tools=research_tools,
        sub_lm=sub_lm,
    )

    Path(run_dir).mkdir(parents=True, exist_ok=True)

    start = time.time()
    try:
        prediction = rlm(query=query)
        findings = prediction.findings or ""
        trajectory = prediction.trajectory if hasattr(prediction, "trajectory") else []
        elapsed = time.time() - start

        result = RlmResult(
            query=query,
            findings=findings,
            trajectory=trajectory,
            iterations=len(trajectory),
            elapsed_seconds=elapsed,
            main_model=main_m,
            sub_model=sub_m,
        )

    except Exception as exc:
        elapsed = time.time() - start
        logger.exception("RLM execution failed")
        result = RlmResult(
            query=query,
            findings="",
            elapsed_seconds=elapsed,
            main_model=main_m,
            sub_model=sub_m,
            error=f"{type(exc).__name__}: {exc}",
        )

    findings_path = Path(run_dir) / "findings.md"
    findings_path.write_text(
        f"# Research: {query}\n\n{result.findings}\n",
        encoding="utf-8",
    )

    meta_path = Path(run_dir) / "rlm_meta.json"
    meta = {
        "query": result.query,
        "iterations": result.iterations,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "main_model": result.main_model,
        "sub_model": result.sub_model,
        "error": result.error,
        "trajectory_length": len(result.trajectory),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    trajectory_path = Path(run_dir) / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(result.trajectory, indent=2, default=str),
        encoding="utf-8",
    )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="RLM Research Runner")
    parser.add_argument("--query", "-q", required=True, help="Research question")
    parser.add_argument("--run-dir", "-d", default="/tmp/rlm-research", help="Output directory")
    parser.add_argument("--max-iterations", type=int, default=15)
    parser.add_argument("--max-llm-calls", type=int, default=30)
    parser.add_argument("--main-model", default=None)
    parser.add_argument("--sub-model", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = run_research(
        args.query,
        run_dir=args.run_dir,
        max_iterations=args.max_iterations,
        max_llm_calls=args.max_llm_calls,
        verbose=args.verbose,
        main_model=args.main_model,
        sub_model=args.sub_model,
    )

    print(json.dumps({
        "status": "error" if result.error else "ok",
        "findings_path": f"{args.run_dir}/findings.md",
        "iterations": result.iterations,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "error": result.error,
    }))

    sys.exit(1 if result.error else 0)


if __name__ == "__main__":
    main()
