"""Generic DSPy RLM execution utilities.

Sagaflow owns the reusable RLM plumbing: model configuration, Deno/Pyodide
setup, tool discovery, and trajectory capture. Skill-specific signatures,
prompts, decomposition, synthesis, and report policy live in their skill
packages.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAIN_MODEL_ENV = "RLM_MAIN_MODEL"
SUB_MODEL_ENV = "RLM_SUB_MODEL"
DEFAULT_MAIN_MODEL = "openai/claude-sonnet-4-6"
DEFAULT_SUB_MODEL = "openai/claude-haiku-4-5"
DENO_PATH = os.environ.get("DENO_PATH", os.path.expanduser("~/.deno/bin/deno"))


@dataclass
class RlmResult:
    """Result from a generic DSPy RLM execution."""

    outputs: dict[str, Any] = field(default_factory=dict)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    elapsed_seconds: float = 0.0
    main_model: str = ""
    sub_model: str = ""
    error: str | None = None

    def output(self, name: str, default: Any = None) -> Any:
        """Return one named output field from the prediction."""
        return self.outputs.get(name, default)


def _ensure_deno_on_path() -> None:
    deno_dir = os.path.dirname(DENO_PATH)
    current_path = os.environ.get("PATH", "")
    if deno_dir and deno_dir not in current_path.split(os.pathsep):
        os.environ["PATH"] = f"{deno_dir}{os.pathsep}{current_path}"


def _api_base() -> str | None:
    return (
        os.environ.get("RLM_API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("CRITIC_BASE_URL")
    )


def _api_key() -> str:
    return (
        os.environ.get("RLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("CRITIC_API_KEY")
        or "sk-dummy"
    )


def _prediction_outputs(prediction: Any) -> dict[str, Any]:
    if hasattr(prediction, "toDict"):
        raw = prediction.toDict()
    elif isinstance(prediction, Mapping):
        raw = dict(prediction)
    else:
        raw = {
            k: v
            for k, v in vars(prediction).items()
            if not k.startswith("_")
        }
    return {
        k: v
        for k, v in raw.items()
        if k not in {"trajectory", "final_reasoning"}
    }


def run_rlm(
    signature: Any,
    inputs: Mapping[str, Any],
    *,
    max_iterations: int = 15,
    max_llm_calls: int = 30,
    max_output_chars: int = 10_000,
    verbose: bool = False,
    tools: list | None = None,
    main_model: str | None = None,
    sub_model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> RlmResult:
    """Execute a DSPy RLM signature with sandboxed code execution.

    Args:
        signature: A ``dspy.Signature`` subclass or DSPy signature string.
        inputs: Input field values passed to the RLM prediction call.
        max_iterations: Maximum REPL/code-execution iterations.
        max_llm_calls: Maximum sub-LLM calls available inside the sandbox.
        max_output_chars: Maximum REPL output preserved per iteration.
        verbose: Enable DSPy RLM verbose logging.
        tools: Tool callables exposed to the sandbox. Defaults to discovered tools.
        main_model: Override the strategy/root LM model ID.
        sub_model: Override the sub-query LM model ID.
        api_base: Override OpenAI-compatible API base URL.
        api_key: Override API key/header value.

    Returns:
        ``RlmResult`` containing named outputs, trajectory, and metadata.
    """
    import dspy

    from sagaflow.rlm.tools import discover_tools

    _ensure_deno_on_path()

    base = api_base or _api_base()
    if not base:
        raise ValueError(
            "RLM_API_BASE environment variable is required. "
            "Set it to your OpenAI-compatible API base URL."
        )

    key = api_key or _api_key()
    main_m = main_model or os.environ.get(MAIN_MODEL_ENV, DEFAULT_MAIN_MODEL)
    sub_m = sub_model or os.environ.get(SUB_MODEL_ENV, DEFAULT_SUB_MODEL)

    main_lm = dspy.LM(main_m, api_base=base, api_key=key)
    sub_lm = dspy.LM(sub_m, api_base=base, api_key=key)
    dspy.configure(lm=main_lm)

    rlm_tools = tools if tools is not None else discover_tools()
    rlm = dspy.RLM(
        signature,
        max_iterations=max_iterations,
        max_llm_calls=max_llm_calls,
        max_output_chars=max_output_chars,
        verbose=verbose,
        tools=rlm_tools,
        sub_lm=sub_lm,
    )

    start = time.time()
    try:
        prediction = rlm(**dict(inputs))
        trajectory = getattr(prediction, "trajectory", []) or []
        return RlmResult(
            outputs=_prediction_outputs(prediction),
            trajectory=trajectory,
            iterations=len(trajectory),
            elapsed_seconds=time.time() - start,
            main_model=main_m,
            sub_model=sub_m,
        )
    except Exception as exc:
        logger.exception("RLM execution failed")
        partial_trajectory = getattr(rlm, "trajectory", None) or []
        return RlmResult(
            trajectory=partial_trajectory,
            iterations=len(partial_trajectory),
            elapsed_seconds=time.time() - start,
            main_model=main_m,
            sub_model=sub_m,
            error=f"{type(exc).__name__}: {exc}",
        )


def load_signature(spec: str) -> Any:
    """Load a signature from ``module:attribute`` or return a DSPy signature string."""
    if ":" not in spec:
        return spec
    module_name, attr_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic DSPy RLM runner")
    parser.add_argument("--signature", required=True, help="DSPy signature string or module:attr")
    parser.add_argument("--input-json", required=True, help="JSON object containing signature inputs")
    parser.add_argument("--run-dir", "-d", default="/tmp/rlm-run", help="Output directory")
    parser.add_argument("--max-iterations", type=int, default=15)
    parser.add_argument("--max-llm-calls", type=int, default=30)
    parser.add_argument("--max-output-chars", type=int, default=10_000)
    parser.add_argument("--main-model", default=None)
    parser.add_argument("--sub-model", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    inputs = json.loads(args.input_json)
    if not isinstance(inputs, dict):
        raise SystemExit("--input-json must decode to a JSON object")

    result = run_rlm(
        load_signature(args.signature),
        inputs,
        max_iterations=args.max_iterations,
        max_llm_calls=args.max_llm_calls,
        max_output_chars=args.max_output_chars,
        verbose=args.verbose,
        main_model=args.main_model,
        sub_model=args.sub_model,
    )

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prediction.json").write_text(
        json.dumps(result.outputs, indent=2, default=str),
        encoding="utf-8",
    )
    (run_dir / "trajectory.json").write_text(
        json.dumps(result.trajectory, indent=2, default=str),
        encoding="utf-8",
    )
    (run_dir / "rlm_meta.json").write_text(
        json.dumps(
            {
                "iterations": result.iterations,
                "elapsed_seconds": round(result.elapsed_seconds, 2),
                "main_model": result.main_model,
                "sub_model": result.sub_model,
                "error": result.error,
                "trajectory_length": len(result.trajectory),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps({
        "status": "error" if result.error else "ok",
        "prediction_path": str(run_dir / "prediction.json"),
        "iterations": result.iterations,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "error": result.error,
    }))
    raise SystemExit(1 if result.error else 0)


if __name__ == "__main__":
    main()
