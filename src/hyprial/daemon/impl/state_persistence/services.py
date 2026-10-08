"""Typed desired-port helpers for service-connect durable mutations."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from hyprial.daemon.impl.desired_state import ServiceRegistry
from .commands import RemoveServiceConnection, SetServiceConnection, SetServiceRegistry
from .commands import _snapshot_payload


class ServiceDesiredPortMixin:
    """Adds service mutations without creating a second persistence lane."""

    def set_service_connection(self, name: str, local_port: int):
        return self._owner._call(
            SetServiceConnection(
                f"state-service-set-{uuid4().hex}", name, local_port
            )
        )

    def remove_service_connection(self, name: str):
        return self._owner._call(
            RemoveServiceConnection(f"state-service-remove-{uuid4().hex}", name)
        )

    def set_service_registry(self, cache: Mapping[str, object] | ServiceRegistry | None):
        if cache is None or isinstance(cache, ServiceRegistry):
            frozen = cache
        elif isinstance(cache, Mapping):
            frozen = ServiceRegistry.from_json(cache)
        else:
            raise TypeError("service registry must be a mapping, ServiceRegistry, or None")
        return self._owner._call(
            SetServiceRegistry(
                f"state-service-registry-{uuid4().hex}",
                _snapshot_payload(frozen),
            )
        )
