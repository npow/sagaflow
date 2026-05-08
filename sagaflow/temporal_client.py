"""Temporal client connection + preflight check."""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client

DEFAULT_TARGET = os.environ.get("SAGAFLOW_TEMPORAL_TARGET", "localhost:7233")
DEFAULT_NAMESPACE = os.environ.get("SAGAFLOW_TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("SAGAFLOW_TEMPORAL_TASK_QUEUE", "sagaflow")


class TemporalUnreachable(RuntimeError):
    """Raised when the Temporal server isn't reachable within the probe deadline."""


def _use_cloud(namespace: str) -> bool:
    """Detect whether to route through Netflix Temporal Cloud (nflx-temporal mTLS path).

    True when SAGAFLOW_TEMPORAL_CLOUD is truthy OR the namespace ends in the
    canonical ``.hzun2`` suffix Netflix uses for managed namespaces.
    """
    flag = os.environ.get("SAGAFLOW_TEMPORAL_CLOUD", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    return namespace.endswith(".hzun2")


async def connect(
    target: str = DEFAULT_TARGET,
    namespace: str = DEFAULT_NAMESPACE,
    timeout_seconds: float = 5.0,
) -> Client:
    if _use_cloud(namespace):
        from nflx_temporal.temporal_client import create_temporal_client
        try:
            return await asyncio.wait_for(
                create_temporal_client(namespace),
                timeout=max(timeout_seconds, 30.0),
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            raise TemporalUnreachable(
                f"Netflix Temporal Cloud namespace {namespace!r} not reachable: {exc}"
            ) from exc
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
