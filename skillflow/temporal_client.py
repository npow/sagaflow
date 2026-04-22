"""Temporal client connection + preflight check."""

from __future__ import annotations

import asyncio

from temporalio.client import Client

DEFAULT_TARGET = "localhost:7233"
DEFAULT_NAMESPACE = "default"
TASK_QUEUE = "skillflow"


class TemporalUnreachable(RuntimeError):
    """Raised when the Temporal server isn't reachable within the probe deadline."""


async def connect(
    target: str = DEFAULT_TARGET,
    namespace: str = DEFAULT_NAMESPACE,
    timeout_seconds: float = 5.0,
) -> Client:
    try:
        return await asyncio.wait_for(
            Client.connect(target, namespace=namespace),
            timeout=timeout_seconds,
        )
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        raise TemporalUnreachable(
            f"Temporal server at {target} not reachable within {timeout_seconds}s: {exc}. "
            f"Start with `temporal server start-dev` and retry."
        ) from exc


async def preflight(
    target: str = DEFAULT_TARGET,
    namespace: str = DEFAULT_NAMESPACE,
    timeout_seconds: float = 2.0,
) -> None:
    """Cheap probe: connect + describe_namespace. Raises TemporalUnreachable on failure."""

    client = await connect(target=target, namespace=namespace, timeout_seconds=timeout_seconds)
    try:
        await asyncio.wait_for(
            client.service_client.workflow_service.describe_namespace(
                _make_describe_namespace_request(namespace)
            ),
            timeout=timeout_seconds,
        )
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        raise TemporalUnreachable(
            f"Temporal describe_namespace({namespace}) at {target} failed: {exc}"
        ) from exc


def _make_describe_namespace_request(namespace: str):  # type: ignore[no-untyped-def]
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest

    return DescribeNamespaceRequest(namespace=namespace)
