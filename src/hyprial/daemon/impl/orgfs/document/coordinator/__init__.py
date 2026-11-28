# Decision record (phase D, package D1, 2026-11-25):
# - Design §9 "dual host" / plan §3.3: the state host composes the new mixins
#   in front of the retained shared mixins, without the content-purge mixin.
# - Package S (2026-11-25): the journal host is deleted and this class took
#   over the public name ``LocalOrgFs`` (``orgfs/docs.py`` re-exports it).
#   The journal-only instance containers (space authorities, pending outbox,
#   purge plans, the store registry) are gone with the code that read them.
# - The store factory is ``factory(space_id, create) -> StateStore``; the
#   default keeps one ``MemoryStateStore`` backing per space (D1 scope; D2
#   passes the file store).  ``store_discard`` forgets a failed creation.
"""The commit coordinator and the state-backed OrgFS facade (design §2.5)."""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from hyprial.daemon.impl.orgfs.composition.vocabulary import OrgFsActorHostCustody
from hyprial.daemon.impl.orgfs.document.coordinator.commit import StateCommit
from hyprial.daemon.impl.orgfs.document.coordinator.host import (
    AdmissionViewOf,
    SpaceSyncHost,
    StateDispatch,
)
from hyprial.daemon.impl.orgfs.document.coordinator.registry import CONTENT_CAPACITY, DocRegistry
from hyprial.daemon.impl.orgfs.document.coordinator.remote import StateRemote
from hyprial.daemon.impl.orgfs.document.coordinator.spaces import (
    MemoryStores,
    StateSpaces,
)
from hyprial.daemon.impl.orgfs.document.coordinator.tree_ops import StateTreeOps
from hyprial.daemon.impl.orgfs.document.effects import FacadeEffects
from hyprial.daemon.impl.orgfs.document.operations.content import FacadeContent
from hyprial.daemon.impl.orgfs.document.operations.spaces import FacadeSpaces
from hyprial.daemon.impl.orgfs.document.operations.tree import FacadeTreeHistory
from hyprial.daemon.impl.orgfs.document.projection import FacadeProjection
from hyprial.daemon.impl.orgfs.document.space_state import FacadeSpaceState
from hyprial.daemon.impl.orgfs.membership import ResultStateAdmission
from hyprial.daemon.impl.orgfs.state import StateStore


def _silent(_level: str, _event: str, **_fields: object) -> None:
    return None


class LocalOrgFs(
    StateDispatch,
    StateCommit,
    StateRemote,
    StateSpaces,
    StateTreeOps,
    FacadeEffects,
    FacadeSpaceState,
    FacadeProjection,
    FacadeSpaces,
    FacadeContent,
    FacadeTreeHistory,
):
    """OrgFS facade over ``StateStore`` / ``Admission`` with a commit coordinator."""

    def __init__(
        self,
        *,
        author: str = "user:local",
        actor: str | None = None,
        node_id: str = "local",
        blobs: Any = None,
        store_factory: Callable[[str, bool], StateStore] | None = None,
        store_discard: Callable[[str], None] | None = None,
        actor_host: Any = None,
        logger: Callable[..., None] | None = None,
        admission: ResultStateAdmission | None = None,
        content_capacity: int = CONTENT_CAPACITY,
    ) -> None:
        if store_factory is None:
            memory = MemoryStores()
            store_factory, store_discard = memory, memory.discard
        self.mesh = None
        self.blobs = blobs
        self.author = author
        self.actor = actor
        self.node_id = node_id
        self._store_factory = store_factory
        self._store_discard = store_discard
        self._logger = logger or _silent
        self._admission = admission or ResultStateAdmission()
        self._content_capacity = content_capacity
        self._process_token = uuid.uuid4().hex[:12]
        self._registries: dict[str, DocRegistry] = {}
        self._syncs: dict[str, Any] = {}
        self._joining: set[str] = set()
        self._refused_spaces: dict[str, tuple[str, dict[str, object]]] = {}
        self._publish_queues: dict[str, Any] = {}
        self._publish_dropped: dict[str, int] = {}
        self._actor_hosts = OrgFsActorHostCustody(actor_host)
        self._lock = threading.RLock()
        self._space_locks: dict[str, threading.RLock] = {}
        self._space_state_context = threading.local()
        self._space_state_owners: dict[str, Any] = {}
        self._detached_spaces: set[str] = set()
        self._space_state_closing = False
        self._effect_context = threading.local()
        self._effect_sequence = 0
        self._effect_failures: list[str] = []
        self._effect_waiters: dict[str, threading.Event] = {}
        self._effect_generation = 1
        self._effect_cell: Any = None
        self._effect_lane: Any = None
        self._network_effect_lane: Any = None
        self._network_effect_stop = threading.Event()
        self._network_effect_draining = False
        self._network_effect_spaces: dict[str, str] = {}
        self._network_due_spaces: set[str] = set()
        self._network_active_spaces: set[str] = set()
        self._effect_closed = False
        self._watchers: dict[str, Any] = {}
        self._meta_watchers: dict[str, Any] = {}
        self._spaces: dict[str, Any] = {}
        self._clock = 0
        self._events: dict[str, list[Any]] = {}


__all__ = [
    "AdmissionViewOf",
    "MemoryStores",
    "SpaceSyncHost",
    "LocalOrgFs",
]
