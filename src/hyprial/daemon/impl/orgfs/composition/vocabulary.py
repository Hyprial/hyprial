# Decision record (phase D, package S, 2026-11-25):
# - The journal store registry is deleted with ``storage/store``; the state
#   runtime's registry is ``composition/state/stores.py``.
# - The announce and directory anti-entropy constants moved here from the
#   deleted journal ``network/protocol.py``; ``composition/directory.py``
#   cannot grow (1000-line gate).  The daemon's maintenance scheduler drives
#   directory anti-entropy at the registered interval (OrgFS owns no polling
#   loop); each node spreads its deadline by up to ±20% so members that
#   started together do not pull in lockstep.
"""Shared vocabulary of the OrgFS runtime composition."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import TYPE_CHECKING, Callable, Final, Protocol

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.api import Registration
    from hyprial.daemon.impl.composition.actors import ActorHostRegistry
    from hyprial.kernel import ActorHost, EffectLane

from hyprial.kernel import ActorCell
from hyprial.daemon.impl.transport import TransportSample


class _SpaceMetaWatcher(Protocol):
    """Package-internal: observe committed changes to a space's meta doc."""

    def watch_meta(
        self, space_id: str, callback: Callable[[], None]
    ) -> Registration: ...


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


_DirectoryCallOperation = _JoinSpace | _CheckoutSpace | _OpenSpaceMesh


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
    runtime: ActorCell
    effects: EffectLane[_LifecycleCommand, _LifecycleOutcome]
    generation: int = 1
    commands: dict[str, _LifecycleCommand] = field(default_factory=dict)
    submitted: set[str] = field(default_factory=set)
    waiters: dict[str, _LifecycleWaiter] = field(default_factory=dict)
    mesh_opening: dict[str, str] = field(default_factory=dict)
    mesh_aliases: dict[str, list[str]] = field(default_factory=dict)
    deferred: list[_LifecycleCommand] = field(default_factory=list)

ORGFS_CONTENT_WAIT_S: Final[float] = 10.0

ORGFS_ANNOUNCE_BUFFER_LIMIT: Final[int] = 256
ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT: Final[int] = 256
ORGFS_DIRECTORY_SYNC_INTERVAL_SECONDS: Final[float] = 60.0
ORGFS_DIRECTORY_SYNC_JITTER_RATIO: Final[float] = 0.2


#: Backoff between await_content provisioning attempts for one blob.
ORGFS_AWAIT_RETRY_SECONDS = 0.1


_ORGFS_HOST_OWNER = "orgfs"


class OrgFsActorHostCustody:
    """The orgfs actor host, injected or privately owned for standalone use.

    OrgFS holds no actor engine: every orgfs actor cell and effect lane comes
    from an injected ActorHost (P4 batch B2). With no host supplied (standalone
    CLI and unit tests) this custody lazily creates one private host registry
    for the "orgfs" owner and owns its shutdown; an injected host is borrowed
    and is never shut down here.
    """

    __slots__ = ("_host", "_lock", "_registry")

    def __init__(self, host: ActorHost | None = None) -> None:
        self._host = host
        self._registry: ActorHostRegistry | None = None
        self._lock = threading.Lock()

    @property
    def host(self) -> ActorHost:
        with self._lock:
            if self._host is None:
                from hyprial.daemon.impl.composition.actors import ActorHostRegistry

                self._registry = ActorHostRegistry()
                self._host = self._registry.host_for(_ORGFS_HOST_OWNER)
            return self._host

    def shutdown(self, timeout: float = 5.0) -> bool:
        with self._lock:
            registry = self._registry
        if registry is None:
            return True
        if not registry.shutdown(timeout):
            return False
        with self._lock:
            if self._registry is registry:
                self._registry = None
                self._host = None
        return True


