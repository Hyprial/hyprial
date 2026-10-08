"""Daemon-owned composition for the P1 orgfs store, mesh, and local facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
import threading
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

from hyprial.kernel import (
    ActorHandle,
    ActorRuntime)
from hyprial.kernel import EffectLane
from hyprial.daemon.impl.transport import TransportSample

from hyprial.daemon.impl.orgfs.storage.store  import LocalSpaceStore


@dataclass(frozen=True, slots=True)
class _DirectoryCommand:
    operation_id: str
    generation: int
    action: str
    sample: TransportSample


@dataclass(frozen=True, slots=True)
class _DirectoryEffect:
    action: str
    peer: str | None = None
    space_ids: tuple[str, ...] = ()
    details: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _DirectoryEffectBatch:
    effects: tuple[_DirectoryEffect, ...]


@dataclass(frozen=True, slots=True)
class _JoinSpace:
    space_id: str


@dataclass(frozen=True, slots=True)
class _ServeSpace:
    space_id: str
    backend: str


@dataclass(frozen=True, slots=True)
class _CheckoutSpace:
    space_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class _OpenSpaceMesh:
    space_id: str


@dataclass(frozen=True, slots=True)
class _SpaceResourceHandle:
    space_id: str
    generation: int


@dataclass(slots=True)
class _MeshCreation:
    done: threading.Event = field(default_factory=threading.Event)
    handle: _SpaceResourceHandle | None = None
    error: BaseException | None = None


_DirectoryCallOperation = _JoinSpace | _ServeSpace | _CheckoutSpace | _OpenSpaceMesh


@dataclass(frozen=True, slots=True)
class _DirectoryCall:
    operation_id: str
    generation: int
    operation: _DirectoryCallOperation


@dataclass(frozen=True, slots=True)
class _DirectoryCallOutcome:
    value: object = None
    error: str | None = None


@dataclass(slots=True)
class _DirectoryWaiter:
    done: threading.Event
    value: object = None


@dataclass(frozen=True, slots=True)
class _LifecycleFailure:
    kind: str
    code: str
    message: str
    details: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _LifecycleOutcome:
    value: object = None
    error: _LifecycleFailure | None = None


@dataclass(frozen=True, slots=True)
class _LifecycleCommand:
    operation_id: str
    generation: int
    operation: _DirectoryCallOperation


@dataclass(slots=True)
class _LifecycleWaiter:
    done: threading.Event
    value: object = None
    error: _LifecycleFailure | None = None


@dataclass(slots=True)
class _LifecycleAuthority:
    runtime: ActorRuntime
    actor: ActorHandle
    effects: EffectLane[_LifecycleCommand, _LifecycleOutcome]
    generation: int = 1
    commands: dict[str, _LifecycleCommand] = field(default_factory=dict)
    submitted: set[str] = field(default_factory=set)
    waiters: dict[str, _LifecycleWaiter] = field(default_factory=dict)
    mesh_opening: dict[str, str] = field(default_factory=dict)
    mesh_aliases: dict[str, list[str]] = field(default_factory=dict)
    deferred: list[_LifecycleCommand] = field(default_factory=list)

ORGFS_CONTENT_WAIT_S: Final[float] = 10.0


class _StoreRegistry(dict[str, LocalSpaceStore]):
    def __init__(self, runtime: "OrgFsRuntime") -> None:
        super().__init__()
        self.runtime = runtime
        self._entries_lock = Lock()
        self._space_locks: dict[str, Lock] = {}

    def _space_lock(self, key: str) -> Lock:
        with self._entries_lock:
            return self._space_locks.setdefault(key, Lock())

    def get(self, key: str, default: Any = None) -> LocalSpaceStore:
        lock = self._space_lock(key)
        with lock:
            with self._entries_lock:
                existing = dict.get(self, key)
            if existing is not None:
                return existing
            created = LocalSpaceStore(
                self.runtime.state_dir,
                key,
                node_id=self.runtime.node_id,
                blob_store=self.runtime.blobs,
            )
            with self._entries_lock:
                existing = dict.get(self, key)
                if existing is None:
                    dict.__setitem__(self, key, created)
                    return created
            created.close()
            return existing

    def snapshot_ids(self) -> tuple[str, ...]:
        with self._entries_lock:
            return tuple(self.keys())

    def snapshot(self) -> tuple[LocalSpaceStore, ...]:
        with self._entries_lock:
            return tuple(self.values())

    def clear(self) -> None:
        with self._entries_lock:
            super().clear()


#: Backoff between await_content provisioning attempts for one blob.
ORGFS_AWAIT_RETRY_SECONDS = 0.1


