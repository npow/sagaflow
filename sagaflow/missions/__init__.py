"""sagaflow.missions — durable mission-enforced agent runner with criteria verification.

Absorbed from swarmd (the standalone swarm orchestrator) into sagaflow as
the ``missions`` subpackage. Contains:

* ``workflow.MissionWorkflow`` — the parent workflow per mission
* ``state.MissionState`` / ``CriterionState`` / ``SpawnTree`` — carry state
* ``specialists/`` — PatternDetector, LLMCritic, ResourceMonitor child workflows
* ``activities/`` — all mission-specific Temporal activities
* ``schemas/`` — Mission, Criterion, Finding, Event, Intervention, Lock
* ``errors`` / ``retry_policies`` — classified error taxonomy + per-activity retry budgets
* ``lib/`` — shared helpers (llm_client, paths)
* ``classifier/`` — prompt classifier (rules + LLM)
"""

from sagaflow.missions.workflow import MissionWorkflow
from sagaflow.missions.state import CriterionState, MissionState, SpawnTree

__all__ = [
    "CriterionState",
    "MissionState",
    "MissionWorkflow",
    "SpawnTree",
]
