from __future__ import annotations

from collections.abc import Callable, Mapping

from hyprial.kernel import (
    ActorCell,
    ActorEngineConfig,
    ActorHost,
    ActorSpec,
    AdmissionResult,
    SupervisionPolicy,
    create_actor_host,
)
from hyprial.kernel.impl.actor_runtime import ActorSnapshot

TellOverride = Callable[[ActorCell, object], AdmissionResult | None]
SnapshotOverride = Callable[[ActorCell, ActorSnapshot], ActorSnapshot]


def new_actor_host(
    *,
    policies: Mapping[str, SupervisionPolicy] | None = None,
    random_source: Callable[[], float] | None = None,
) -> ActorHost:
    return create_actor_host(
        ActorEngineConfig(policies=policies, random_source=random_source)
    )


class InterceptingCell(ActorCell):
    def __init__(
        self, engine: object, handle: object, kind: str, owner: InterceptingHost
    ) -> None:
        super().__init__(engine, handle, kind)  # type: ignore[arg-type]
        self._owner = owner

    def tell(self, command: object) -> AdmissionResult:
        override = self._owner.override_tell(self, command)
        if override is not None:
            return override
        return super().tell(command)

    def snapshot(self) -> ActorSnapshot:
        return self._owner.override_snapshot(self, super().snapshot())


class CellProxy:
    """Assignable cell wrapper for tests that patch tell or snapshot."""

    def __init__(self, cell: ActorCell) -> None:
        object.__setattr__(self, "_cell", cell)

    def __getattr__(self, name: str) -> object:
        return getattr(self._cell, name)


def wrap_cell(owner: object) -> CellProxy:
    proxy = CellProxy(owner._cell)  # type: ignore[attr-defined]
    owner._cell = proxy  # type: ignore[attr-defined]
    return proxy


class InterceptingHost:
    """Host wrapper whose spawned cells expose injectable tell/snapshot seams."""

    def __init__(self, host: ActorHost | None = None) -> None:
        self._host = new_actor_host() if host is None else host
        self._tell_override: TellOverride | None = None
        self._snapshot_override: SnapshotOverride | None = None

    def intercept_tell(self, override: TellOverride | None) -> None:
        self._tell_override = override

    def intercept_snapshot(self, override: SnapshotOverride | None) -> None:
        self._snapshot_override = override

    def override_tell(
        self, cell: ActorCell, command: object
    ) -> AdmissionResult | None:
        override = self._tell_override
        if override is None:
            return None
        return override(cell, command)

    def override_snapshot(
        self, cell: ActorCell, snapshot: ActorSnapshot
    ) -> ActorSnapshot:
        override = self._snapshot_override
        if override is None:
            return snapshot
        return override(cell, snapshot)

    def spawn(self, spec: ActorSpec) -> ActorCell:
        cell = self._host.spawn(spec)
        return InterceptingCell(cell._engine, cell.handle, cell.kind, self)

    def __getattr__(self, name: str) -> object:
        return getattr(self._host, name)
