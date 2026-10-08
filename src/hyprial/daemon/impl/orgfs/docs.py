from __future__ import annotations












import threading




from typing import Any


from hyprial.kernel import (
    ActorHandle,
    ActorRuntime)

from hyprial.kernel import EffectLane




from hyprial.daemon.impl.orgfs.api  import (
    ChangeEvent)


from hyprial.daemon.impl.orgfs.projection.purge  import (
    PurgePlan)


from hyprial.daemon.impl.orgfs.storage.space_authority  import (
    OrgSpaceAuthority)



from hyprial.daemon.impl.orgfs.storage.store  import CommitRecord

from hyprial.daemon.impl.orgfs.document.effects import FacadeEffects
from hyprial.daemon.impl.orgfs.document.space_state import FacadeSpaceState
from hyprial.daemon.impl.orgfs.document.projection import FacadeProjection
from hyprial.daemon.impl.orgfs.document.operations.spaces import FacadeSpaces
from hyprial.daemon.impl.orgfs.document.operations.content import FacadeContent
from hyprial.daemon.impl.orgfs.document.operations.tree import FacadeTreeHistory
from hyprial.daemon.impl.orgfs.document.operations.purge import FacadePurge
from hyprial.daemon.impl.orgfs.document.model import ORGFS_TEXT_MAX as ORGFS_TEXT_MAX
from hyprial.daemon.impl.orgfs.document.model import TreeDocument as TreeDocument
from hyprial.daemon.impl.orgfs.document.model import _AcknowledgePurge as _AcknowledgePurge
from hyprial.daemon.impl.orgfs.document.model import _ApplyContentUpdate as _ApplyContentUpdate
from hyprial.daemon.impl.orgfs.document.model import _ApplyEnvelope as _ApplyEnvelope
from hyprial.daemon.impl.orgfs.document.model import _ApplyStructuredUpdate as _ApplyStructuredUpdate
from hyprial.daemon.impl.orgfs.document.model import _ApplyTreeUpdate as _ApplyTreeUpdate
from hyprial.daemon.impl.orgfs.document.model import _BroadcastPending as _BroadcastPending
from hyprial.daemon.impl.orgfs.document.model import _ContentDocument as _ContentDocument
from hyprial.daemon.impl.orgfs.document.model import _CreateSpace as _CreateSpace
from hyprial.daemon.impl.orgfs.document.model import _FACADE_EFFECT_CAPACITY as _FACADE_EFFECT_CAPACITY
from hyprial.daemon.impl.orgfs.document.model import _FacadeEffectBatch as _FacadeEffectBatch
from hyprial.daemon.impl.orgfs.document.model import _FacadeEffectCompletion as _FacadeEffectCompletion
from hyprial.daemon.impl.orgfs.document.model import _FacadeEffectResult as _FacadeEffectResult
from hyprial.daemon.impl.orgfs.document.model import _History as _History
from hyprial.daemon.impl.orgfs.document.model import _HydrateContentSnapshot as _HydrateContentSnapshot
from hyprial.daemon.impl.orgfs.document.model import _InstallReplacement as _InstallReplacement
from hyprial.daemon.impl.orgfs.document.model import _InviteMember as _InviteMember
from hyprial.daemon.impl.orgfs.document.model import _LoadSpace as _LoadSpace
from hyprial.daemon.impl.orgfs.document.model import _MUTATING_FACADE_METHODS as _MUTATING_FACADE_METHODS
from hyprial.daemon.impl.orgfs.document.model import _Mkdir as _Mkdir
from hyprial.daemon.impl.orgfs.document.model import _Move as _Move
from hyprial.daemon.impl.orgfs.document.model import _Node as _Node
from hyprial.daemon.impl.orgfs.document.model import _NodeSnapshot as _NodeSnapshot
from hyprial.daemon.impl.orgfs.document.model import _OrgDoc as _OrgDoc
from hyprial.daemon.impl.orgfs.document.model import _Purge as _Purge
from hyprial.daemon.impl.orgfs.document.model import _PurgePlanCommand as _PurgePlanCommand
from hyprial.daemon.impl.orgfs.document.model import _ReconcileReplicaBlobs as _ReconcileReplicaBlobs
from hyprial.daemon.impl.orgfs.document.model import _RemoveMember as _RemoveMember
from hyprial.daemon.impl.orgfs.document.model import _RemoveNode as _RemoveNode
from hyprial.daemon.impl.orgfs.document.model import _Restore as _Restore
from hyprial.daemon.impl.orgfs.document.model import _SPACE_STATE_CAPACITY as _SPACE_STATE_CAPACITY
from hyprial.daemon.impl.orgfs.document.model import _SPACE_STATE_METHODS as _SPACE_STATE_METHODS
from hyprial.daemon.impl.orgfs.document.model import _Space as _Space
from hyprial.daemon.impl.orgfs.document.model import _SpaceStateCommand as _SpaceStateCommand
from hyprial.daemon.impl.orgfs.document.model import _SpaceStateFailure as _SpaceStateFailure
from hyprial.daemon.impl.orgfs.document.model import _SpaceStateOperation as _SpaceStateOperation
from hyprial.daemon.impl.orgfs.document.model import _SpaceStateOutcome as _SpaceStateOutcome
from hyprial.daemon.impl.orgfs.document.model import _SpaceStateOwner as _SpaceStateOwner
from hyprial.daemon.impl.orgfs.document.model import _SpaceStateWaiter as _SpaceStateWaiter
from hyprial.daemon.impl.orgfs.document.model import _Unban as _Unban
from hyprial.daemon.impl.orgfs.document.model import _Watch as _Watch
from hyprial.daemon.impl.orgfs.document.model import _WatchNotification as _WatchNotification
from hyprial.daemon.impl.orgfs.document.model import _WriteBytes as _WriteBytes
from hyprial.daemon.impl.orgfs.document.model import _WriteText as _WriteText
from hyprial.daemon.impl.orgfs.document.model import _decode_content_frontier as _decode_content_frontier
from hyprial.daemon.impl.orgfs.document.model import _encode_content_frontier as _encode_content_frontier
from hyprial.daemon.impl.orgfs.document.model import _facade_locked as _facade_locked
from hyprial.daemon.impl.orgfs.document.model import _node_uri as _node_uri
from hyprial.daemon.impl.orgfs.document.model import _now as _now
from hyprial.daemon.impl.orgfs.document.model import _raise_invalid as _raise_invalid
from hyprial.daemon.impl.orgfs.document.model import _version as _version
from hyprial.daemon.impl.orgfs.document.model import _version_number as _version_number


class LocalOrgFs(FacadeEffects, FacadeSpaceState, FacadeProjection, FacadeSpaces, FacadeContent, FacadeTreeHistory, FacadePurge):
    """Reference local facade used by the daemon and by isolated tests.

    ``stores`` and ``blobs`` are optional duck-typed injections.  When a
    SpaceStore is supplied every mutation is submitted through its ``commit``
    method; the local fallback is intentionally only an in-process test
    backend and has no filesystem side effects.

    Advanced purge and replacement-snapshot operations require a durable
    store implementing ``commit_with_outbox`` so its typed authority owns
    the operation. Commit-only test stores cannot perform those operations.
    Instance overrides of a class-defined ``broadcast_pending`` are captured
    as broadcast suppression; their callback invocation count is not promised.
    """


    def __init__(
        self,
        stores: Any = None,
        blobs: Any = None,
        mesh: Any = None,
        *,
        author: str = "user:local",
        actor: str | None = None,
        node_id: str = "local",
    ) -> None:
        self.stores = stores
        self.blobs = blobs
        self.mesh = mesh
        self.author = author
        self.actor = actor
        self.node_id = node_id
        self._lock = threading.RLock()
        self._space_locks: dict[str, threading.RLock] = {}
        self._space_state_context = threading.local()
        self._space_state_owners: dict[str, _SpaceStateOwner] = {}
        self._space_state_closing = False
        self._effect_context = threading.local()
        self._effect_sequence = 0
        self._effect_failures: list[str] = []
        self._effect_waiters: dict[str, threading.Event] = {}
        self._effect_generation = 1
        self._effect_runtime: ActorRuntime | None = None
        self._effect_actor: ActorHandle | None = None
        self._effect_lane: EffectLane[_FacadeEffectBatch, _FacadeEffectResult] | None = None
        self._effect_closed = False
        self._watchers: dict[str, _Watch] = {}
        self._spaces: dict[str, _Space] = {}
        self._space_authorities: dict[str, OrgSpaceAuthority] = {}
        self._clock = 0
        self._events: dict[str, list[ChangeEvent]] = {}
        self._purge_plans: dict[str, tuple[PurgePlan, tuple[dict[str, str], ...]]] = {}
        self._pending_outbox: dict[str, list[CommitRecord]] = {}
        self._broadcast_inflight: set[tuple[str, int, bytes]] = set()
