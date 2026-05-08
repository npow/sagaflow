"""Temporal client connection + preflight check."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod

from temporalio.client import Client

_log = logging.getLogger(__name__)

DEFAULT_TARGET = os.environ.get("SAGAFLOW_TEMPORAL_TARGET", "localhost:7233")
DEFAULT_NAMESPACE = os.environ.get("SAGAFLOW_TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.environ.get("SAGAFLOW_TEMPORAL_TASK_QUEUE", "sagaflow")
DEFAULT_PROVIDER = os.environ.get("SAGAFLOW_TEMPORAL_PROVIDER", "local").strip().lower()

PROVIDER_ENTRY_POINT_GROUP = "sagaflow.temporal_providers"


class TemporalUnreachable(RuntimeError):
    """Raised when the Temporal server isn't reachable within the probe deadline."""


class TemporalProvider(ABC):
    """Pluggable connector for a Temporal backend.

    Built-in providers ship with sagaflow (``local``). Org-specific providers
    register via the ``sagaflow.temporal_providers`` entry-point group, e.g.
    a ``sagaflow-nflx`` package can register a ``nflx`` provider that handles
    Netflix Temporal Cloud auth.

    Implementations should be cheap to instantiate; the heavy work happens in
    ``connect()`` per-call.
    """

    name: str

    @abstractmethod
    async def connect(
        self,
        target: str,
        namespace: str,
        timeout_seconds: float,
    ) -> Client: ...


class LocalTemporalProvider(TemporalProvider):
    """Connect to an OSS Temporal server via TCP (no TLS, no proxy)."""

    name = "local"

    async def connect(self, target: str, namespace: str, timeout_seconds: float) -> Client:
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


_PROVIDERS: dict[str, TemporalProvider] | None = None


def _load_providers() -> dict[str, TemporalProvider]:
    """Load built-in + entry-point-registered providers.

    Built-ins always win over plugins on name collision (so a third-party
    ``local`` can't shadow the standard one). Entry points that fail to load
    are logged at WARNING and skipped — a broken plugin must be loud, never
    silent, but should not break the worker process.
    """
    providers: dict[str, TemporalProvider] = {"local": LocalTemporalProvider()}
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover — Python <3.10 not supported anyway
        return providers
    try:
        eps = entry_points(group=PROVIDER_ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001 — older importlib.metadata variants
        _log.warning("failed to enumerate %s entry points: %s", PROVIDER_ENTRY_POINT_GROUP, exc)
        return providers
    for ep in eps:
        try:
            cls = ep.load()
            inst = cls() if isinstance(cls, type) else cls
            if not isinstance(inst, TemporalProvider):
                _log.warning(
                    "entry point %s did not return a TemporalProvider; got %r",
                    ep.name, type(inst),
                )
                continue
            if inst.name in providers:
                continue  # built-in or earlier plugin already registered this name
            providers[inst.name] = inst
        except Exception as exc:  # noqa: BLE001
            _log.warning("failed to load temporal provider %s: %s", ep.name, exc)
    return providers


def get_providers() -> dict[str, TemporalProvider]:
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = _load_providers()
    return _PROVIDERS


async def connect(
    target: str = DEFAULT_TARGET,
    namespace: str = DEFAULT_NAMESPACE,
    timeout_seconds: float = 5.0,
    provider: str | None = None,
) -> Client:
    chosen = (provider or DEFAULT_PROVIDER).strip().lower() or "local"
    providers = get_providers()
    impl = providers.get(chosen)
    if impl is None:
        raise TemporalUnreachable(
            f"Unknown temporal provider {chosen!r}. "
            f"Available: {sorted(providers)}. "
            f"Set SAGAFLOW_TEMPORAL_PROVIDER or install a plugin that registers "
            f"a {PROVIDER_ENTRY_POINT_GROUP!r} entry point."
        )
    return await impl.connect(target, namespace, timeout_seconds)


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
