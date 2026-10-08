from __future__ import annotations

from collections.abc import Callable


import json

from pathlib import Path

from threading import Lock, RLock

import threading

import time

from typing import Any



from hyprial.kernel import (
    ActorHandle,
    ActorRuntime)

from hyprial.kernel import EffectLane, EffectRequest

from hyprial.daemon.impl.transport import KeySpace, Registration, TransportSession



from hyprial.daemon.impl.orgfs.storage.blob_authority  import BlobAuthority

from hyprial.daemon.impl.orgfs.projection.checkout_authority  import CheckoutAuthority

from hyprial.daemon.impl.orgfs.docs  import LocalOrgFs

from hyprial.daemon.impl.orgfs.mesh  import (
    OrgFsMesh)


from hyprial.daemon.impl.orgfs.storage.replica_authority  import ReplicaAuthority



from hyprial.daemon.impl.orgfs.composition.directory import RuntimeDirectory
from hyprial.daemon.impl.orgfs.composition.lifecycle import RuntimeLifecycle
from hyprial.daemon.impl.orgfs.composition.content import RuntimeContent
from hyprial.daemon.impl.orgfs.composition.checkout import RuntimeCheckout
from hyprial.daemon.impl.orgfs.composition.vocabulary import ORGFS_AWAIT_RETRY_SECONDS as ORGFS_AWAIT_RETRY_SECONDS
from hyprial.daemon.impl.orgfs.composition.vocabulary import ORGFS_CONTENT_WAIT_S as ORGFS_CONTENT_WAIT_S
from hyprial.daemon.impl.orgfs.composition.vocabulary import _CheckoutSpace as _CheckoutSpace
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryCall as _DirectoryCall
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryCallOperation as _DirectoryCallOperation
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryCallOutcome as _DirectoryCallOutcome
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryCommand as _DirectoryCommand
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryEffect as _DirectoryEffect
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryEffectBatch as _DirectoryEffectBatch
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryWaiter as _DirectoryWaiter
from hyprial.daemon.impl.orgfs.composition.vocabulary import _JoinSpace as _JoinSpace
from hyprial.daemon.impl.orgfs.composition.vocabulary import _LifecycleAuthority as _LifecycleAuthority
from hyprial.daemon.impl.orgfs.composition.vocabulary import _LifecycleCommand as _LifecycleCommand
from hyprial.daemon.impl.orgfs.composition.vocabulary import _LifecycleFailure as _LifecycleFailure
from hyprial.daemon.impl.orgfs.composition.vocabulary import _LifecycleOutcome as _LifecycleOutcome
from hyprial.daemon.impl.orgfs.composition.vocabulary import _LifecycleWaiter as _LifecycleWaiter
from hyprial.daemon.impl.orgfs.composition.vocabulary import _MeshCreation as _MeshCreation
from hyprial.daemon.impl.orgfs.composition.vocabulary import _OpenSpaceMesh as _OpenSpaceMesh
from hyprial.daemon.impl.orgfs.composition.vocabulary import _ServeSpace as _ServeSpace
from hyprial.daemon.impl.orgfs.composition.vocabulary import _SpaceResourceHandle as _SpaceResourceHandle
from hyprial.daemon.impl.orgfs.composition.vocabulary import _StoreRegistry as _StoreRegistry


class OrgFsRuntime(RuntimeDirectory, RuntimeLifecycle, RuntimeContent, RuntimeCheckout):
    """Own all orgfs resources for one daemon generation."""


    def __init__(
        self,
        state_dir: Path,
        *,
        node_id: str,
        author: str,
        actor: str | None = None,
        logger: Callable[..., None] | None = None,
        owner_notifier: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.node_id = node_id
        self.author = author
        self.actor = actor
        self.logger = logger
        self.owner_notifier = owner_notifier
        self.blobs = BlobAuthority(self.state_dir)
        self.stores = _StoreRegistry(self)
        self.facade = LocalOrgFs(
            self.stores,
            self.blobs,
            self,
            author=author,
            actor=actor,
            node_id=node_id,
        )
        self._resource_lock = RLock()
        self._closing = False
        self._closed = False
        self._close_lock = Lock()
        self._checkouts: dict[str, CheckoutAuthority] = {}
        self._checkout_lock = Lock()
        self._checkout_config_lock = Lock()
        self._checkout_closing = False
        self._checkout_config = self.state_dir / "orgfs" / "checkouts.json"
        self._replicas: dict[str, ReplicaAuthority] = {}
        self._replica_creation_locks: dict[str, Lock] = {}
        self._mesh_creations: dict[str, _MeshCreation] = {}
        spaces_root = self.state_dir / "orgfs" / "spaces"
        if spaces_root.is_dir():
            for path in sorted(spaces_root.iterdir()):
                if not path.is_dir() or not (path / "journal.sqlite3").is_file():
                    continue
                self.stores.get(path.name)
                self.facade.load_space(path.name)
        self._restore_checkouts()
        self._session: TransportSession | None = None
        self._supplier_online: Callable[[str], bool] = lambda _supplier: True
        self._holder_candidates: Callable[[], tuple[str, ...]] = lambda: ()
        self._meshes: dict[str, OrgFsMesh] = {}
        self._announce_registration: Registration | None = None
        self._liveliness_registration: Registration | None = None
        self._stop_supplier_online: Callable[[], None] | None = None
        self._stop_rebuild_complete: Callable[[], None] | None = None
        self._sync_after_presence = False
        self._holders: dict[str, set[str]] = {}
        self._known_holders: dict[str, set[str]] = {}
        self._lively_peers: set[str] = set()
        self._pending_announces: dict[str, dict[str, Any]] = {}
        self._deferred_supplier_online: set[str] = set()
        # (space_id, peer) pairs an online edge skipped: the peer was not yet
        # a known holder. Its later announce pulls exactly these, once.
        self._edge_skipped_holdings: set[tuple[str, str]] = set()
        self._pending_announces_lock = Lock()
        self._announce_buffer_dropped = 0
        self._announce_buffer_log_bucket = -1
        self._directory_generation = 1
        self._directory_runtime: ActorRuntime | None = None
        self._directory_actor: ActorHandle | None = None
        self._directory_effects: EffectLane[
            _DirectoryEffectBatch, None
        ] | None = None
        self._directory_closing = False
        self._directory_dropped = 0
        self._directory_custody_lock = Lock()
        self._directory_pending: set[tuple[str, int]] = set()
        self._directory_rebuild_pending = False
        self._directory_sync_due: float | None = None
        # One additional bounded envelope must reach the state owner so a full
        # retained announce buffer can evict its oldest entry and report that
        # semantic drop. Total queued + active + deferred custody remains fixed.
        self._directory_capacity = self._directory_mailbox_capacity()
        self._directory_deferred: list[EffectRequest[_DirectoryEffectBatch]] = []
        self._lifecycle_lock = Lock()
        self._lifecycle_authority: _LifecycleAuthority | None = None
        self._lifecycle_context = threading.local()
        self._lifecycle_closing = False


    def close(self) -> None:
        with self._close_lock:
            with self._resource_lock:
                if self._closed:
                    return
                # Admission is fenced immediately, but remains retryable until
                # every accepted operation and owned resource has drained.
                self._closing = True
            self._close_impl()


    def _close_impl(self) -> None:
        if self._stop_rebuild_complete is not None:
            self._stop_rebuild_complete()
            self._stop_rebuild_complete = None
        if self._stop_supplier_online is not None:
            self._stop_supplier_online()
            self._stop_supplier_online = None
        with self._resource_lock:
            self._closing = True
            creations = tuple(self._mesh_creations.values())
        deadline = time.monotonic() + 5.0
        for creation in creations:
            if not creation.done.wait(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("orgfs mesh creation did not drain")
        if not self._close_lifecycle(timeout=5.0):
            raise TimeoutError("orgfs directory lifecycle did not drain")
        with self._checkout_lock:
            self._checkout_closing = True
            checkouts = tuple(self._checkouts.items())
        for _space_id, checkout in checkouts:
            if isinstance(checkout, CheckoutAuthority) and not checkout.close(timeout=5.0):
                raise TimeoutError("orgfs checkout I/O did not drain")
        with self._checkout_lock:
            self._checkouts.clear()
        if self._session is not None:
            try:
                self._session.put(
                    KeySpace().orgfs_announce(self.node_id),
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "type": "orgfs-announce",
                            "node": self.node_id,
                            "durable": True,
                            "spaces": [],
                        },
                        separators=(",", ":"),
                    ).encode(),
                )
            except Exception:
                pass
        if self._announce_registration is not None:
            self._announce_registration.close()
            self._announce_registration = None
        if self._liveliness_registration is not None:
            self._liveliness_registration.close()
            self._liveliness_registration = None
        if not self._close_directory_ingress(timeout=5.0):
            raise TimeoutError("orgfs directory effects did not drain")
        with self._resource_lock:
            meshes = tuple(self._meshes.values())
            replicas = tuple(self._replicas.values())
        for mesh in reversed(meshes):
            mesh.close()
        for replica in replicas:
            if not replica.close(timeout=5.0):
                raise TimeoutError("orgfs replica authority did not drain")
        if not self.facade.close_effects(timeout=5.0):
            raise TimeoutError("orgfs post-commit effects did not drain")
        with self._resource_lock:
            self._meshes.clear()
            self._replicas.clear()
        with self._pending_announces_lock:
            self._pending_announces.clear()
            self._lively_peers.clear()
        with self._resource_lock:
            self._session = None
        if not self.blobs.close(timeout=5.0):
            raise TimeoutError("orgfs blob authority did not drain")
        for store in self.stores.snapshot():
            store.close()
        self.stores.clear()
        with self._resource_lock:
            self._closed = True


    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = ["OrgFsRuntime"]
