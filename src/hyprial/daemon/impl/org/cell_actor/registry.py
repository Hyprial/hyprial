# Decision notes (OC2 org cell actor registry):
# - Manages one `OrgCellActor` per known org on `app._org_cell_actors`, backed
#   by `app._actor_hosts.host_for("org-cell")` when an `ActorHostRegistry` is
#   present and clean in-process locking when running under minimal fakes.
# - Unknown, left, deleted or foreign orgs are never retained in the registry:
#   transient actors spawned for a lookup or join attempt are evicted if the
#   resulting state shows no active local membership or ownership.
"""Per-app registry and lifecycle helpers for ``OrgCellActor`` instances."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from hyprial.daemon.impl.org.cell_actor.actor import (
    ORG_CELL_ACTOR_GROUP,
    OrgCellActor,
    OrgCellActorSnapshot,
)
from hyprial.daemon.impl.org.cell_actor.sync import (
    list_live_org_names,
    org_ownership,
)


def _host_for_app(app: Any) -> Any:
    hosts = getattr(app, "_actor_hosts", None)
    host_for = getattr(hosts, "host_for", None)
    if not callable(host_for):
        return None
    return host_for(ORG_CELL_ACTOR_GROUP)


class OrgCellActorRegistry:
    """Thread-safe holder of per-org ``OrgCellActor`` instances for one daemon app."""

    def __init__(
        self,
        app: Any,
        *,
        service_factory: Callable[[], Any],
        actor_host: Any = None,
    ) -> None:
        self._app = app
        self._service_factory = service_factory
        self._actor_host = actor_host if actor_host is not None else _host_for_app(app)
        self._lock = threading.RLock()
        self._closed = False
        self._actors: dict[str, OrgCellActor] = {}

    def for_org(self, org: str) -> OrgCellActor:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"org cell actor registry is closed for {org!r}")
            existing = self._actors.get(org)
            if existing is not None:
                return existing
            actor = OrgCellActor(
                org=org,
                app=self._app,
                service_factory=self._service_factory,
                actor_host=self._actor_host,
            )
            self._actors[org] = actor
            return actor

    def evict(self, org: str) -> bool:
        with self._lock:
            actor = self._actors.pop(org, None)
        if actor is None:
            return False
        return actor.close()

    def prune_if_unowned(self, org: str) -> bool:
        """Evict *org* unless it is a live local org space owned or joined by this node."""

        live = set(list_live_org_names(self._app))
        if org in live:
            ownership = org_ownership(self._app, org)
            if ownership.space_id is not None and ownership.role is not None:
                return False
        return self.evict(org)

    def reconcile(self) -> dict[str, OrgCellActorSnapshot]:
        """Synchronize actors to current live local orgs; never keeps unknown or left ones."""

        live = set(list_live_org_names(self._app))
        with self._lock:
            if self._closed:
                return {}
            stale = [name for name in self._actors if name not in live]
        for name in stale:
            self.evict(name)
        for name in sorted(live):
            ownership = org_ownership(self._app, name)
            if ownership.space_id is not None and ownership.role is not None:
                self.for_org(name)
            else:
                self.evict(name)
        return self.snapshots()

    def snapshots(self) -> dict[str, OrgCellActorSnapshot]:
        with self._lock:
            items = list(self._actors.items())
        return {name: actor.snapshot() for name, actor in items}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            actors = list(self._actors.values())
            self._actors.clear()
        for actor in actors:
            actor.close()


_REGISTRY_GUARD = threading.Lock()


def ensure_org_cell_actors(
    app: Any,
    *,
    service_factory: Callable[[], Any] | None = None,
) -> OrgCellActorRegistry:
    """Return or lazily attach the ``OrgCellActorRegistry`` for *app*."""

    existing = getattr(app, "_org_cell_actors", None)
    if existing is not None:
        if service_factory is not None:
            existing._service_factory = service_factory
        return existing
    with _REGISTRY_GUARD:
        existing = getattr(app, "_org_cell_actors", None)
        if existing is not None:
            if service_factory is not None:
                existing._service_factory = service_factory
            return existing
        factory = service_factory
        if factory is None:
            from hyprial.daemon.impl.org.network.ipc import _request_service

            def factory() -> Any:
                return _request_service(app)
        registry = OrgCellActorRegistry(app, service_factory=factory)
        if getattr(app, "_org_cell_actors_closed", False):
            registry.close()
            return registry
        try:
            app._org_cell_actors = registry
        except AttributeError:
            pass
        return registry


def org_cell_actors(app: Any) -> OrgCellActorRegistry | None:
    """Return the already-attached ``OrgCellActorRegistry`` on *app*, or ``None``."""

    return getattr(app, "_org_cell_actors", None)


def close_org_cell_actors(app: Any) -> None:
    """Detach and close the app's ``OrgCellActorRegistry`` if present."""

    try:
        app._org_cell_actors_closed = True
    except AttributeError:
        pass
    registry = getattr(app, "_org_cell_actors", None)
    try:
        app._org_cell_actors = None
    except AttributeError:
        pass
    if registry is not None:
        registry.close()


__all__ = [
    "OrgCellActorRegistry",
    "close_org_cell_actors",
    "ensure_org_cell_actors",
    "org_cell_actors",
]
