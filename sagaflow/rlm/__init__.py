"""RLM (Recursive Language Model) research — DSPy sandbox-based research execution."""

from sagaflow.rlm.tools import BUILTIN_TOOLS, RlmTool, discover_tools, read_file

__all__ = [
    "BUILTIN_TOOLS",
    "RlmTool",
    "discover_tools",
    "read_file",
    "run_deep_research",
]


def run_deep_research(*args, **kwargs):
    from sagaflow.rlm.orchestrator import run_deep_research as _run

    return _run(*args, **kwargs)
