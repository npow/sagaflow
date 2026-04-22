from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sagaflow.registry import SkillRegistry, SkillSpec
from sagaflow.worker import build_registry, _is_worker_reachable


def fake_wf_cls():
    class W:  # pragma: no cover
        pass

    return W


async def test_build_registry_includes_hello_world() -> None:
    registry = build_registry()
    assert "hello-world" in set(registry.names())


async def test_is_worker_reachable_true_when_pollers_exist() -> None:
    fake_client = SimpleNamespace(
        service_client=SimpleNamespace(
            workflow_service=SimpleNamespace(
                describe_task_queue=AsyncMock(
                    return_value=SimpleNamespace(
                        pollers=[SimpleNamespace(identity="worker-1")]
                    )
                )
            )
        )
    )
    reachable = await _is_worker_reachable(fake_client)
    assert reachable is True


async def test_is_worker_reachable_false_when_no_pollers() -> None:
    fake_client = SimpleNamespace(
        service_client=SimpleNamespace(
            workflow_service=SimpleNamespace(
                describe_task_queue=AsyncMock(
                    return_value=SimpleNamespace(pollers=[])
                )
            )
        )
    )
    reachable = await _is_worker_reachable(fake_client)
    assert reachable is False
