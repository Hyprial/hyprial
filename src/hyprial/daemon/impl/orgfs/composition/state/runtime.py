# Decision record (phase D, package D2, 2026-11-25):
# - Design §9 "dual host" / plan §3.3: the state runtime composes the new
#   mixins in front of the retained runtime mixins and owns a ``LocalOrgFs``
#   over one ``FileStateStore`` per space.
# - Package S (2026-11-25): the journal runtime is deleted and this class took
#   over the public name ``OrgFsRuntime`` (``orgfs/runtime.py`` re-exports
#   it); the content mixin of the journal runtime is gone.
# - The facade's ``mesh`` is this runtime (as in the legacy assembly), so
#   ``facade.join`` reaches the runtime's join.
# - Startup (design §2.3, §2.7, §9 R6): classify ``spaces/``; refuse a space
#   whose layout the store rejects (``ORGFS_LAYOUT_UNSUPPORTED``, recorded on
#   the facade so its requests carry the reason) and keep the others; delete
#   an unfinished creation or join through the local purge's deletion fence;
#   ``load_space`` reads meta and tree only.  A scan that raises (a directory
#   not named by a UUID) closes what the constructor already opened.
# - Close (D1 report §11, design §9 R15): fence admissions, drain the
#   lifecycle, checkouts and directory ingress as before, publish the empty
#   announce with ``"protocol": 3``, drain the publication queues, close every
#   ``SpaceSync`` (after which no host call happens), then the facade's
#   effects (which close the stores), the blob authority and the actor host.
# - F1 (design, node gate ruling): the optional ``device_owner_hint`` is the
#   assembly's bootstrap source for the node gate; ``bind_device_owner_hint``
#   sets it after construction for an assembly that builds the runtime class
#   chosen by the rehearsal switch.
# - Package F6 (R3-8): a refused space records the store's explanation (with
#   the migration guide) as the error ``message``, so every later lookup
#   says why and where to look, not only the code.
# - Package F8b: ``_join_locks`` holds one ``_JoinLock`` (lock and user
#   count) per space with a join running or waiting; ``sync.py`` drops the
#   entry with its last user.
"""``OrgFsRuntime``: the OrgFS runtime over the state store and protocol 3 sync."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import threading
from threading import Lock, RLock
import time
from typing import TYPE_CHECKING, Any

from hyprial.kernel import ActorCell, EffectRequest, ipc_errors

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.composition.checkout import RuntimeCheckout
from hyprial.daemon.impl.orgfs.composition.directory import RuntimeDirectory
from hyprial.daemon.impl.orgfs.composition.lifecycle import RuntimeLifecycle
from hyprial.daemon.impl.orgfs.composition.space_purge import RuntimeSpacePurge
from hyprial.daemon.impl.orgfs.composition.state.content import StateContent
from hyprial.daemon.impl.orgfs.composition.state.stores import StateStoreRegistry, classify_spaces
from hyprial.daemon.impl.orgfs.composition.state.sync import StateLifecycle, _JoinLock
from hyprial.daemon.impl.orgfs.composition.vocabulary import (
    OrgFsActorHostCustody,
    _DirectoryEffectBatch,
    _LifecycleAuthority,
    _MeshCreation,
)
from hyprial.daemon.impl.orgfs.document.coordinator import LocalOrgFs
from hyprial.daemon.impl.orgfs.instrumentation import AnnounceInstrumentation
from hyprial.daemon.impl.orgfs.network.supplier_health import SupplierSyncHealth
from hyprial.daemon.impl.orgfs.projection.checkout_authority import CheckoutAuthority
from hyprial.daemon.impl.orgfs.state import FileStateStore
from hyprial.daemon.impl.orgfs.storage.blob_authority import BlobAuthority
from hyprial.daemon.impl.orgfs.storage.vocabulary import StoreError
from hyprial.daemon.impl.transport import KeySpace, Registration, TransportSession

if TYPE_CHECKING:
    from hyprial.kernel import ActorHost, EffectLane

CLOSE_TIMEOUT_SECONDS = 5.0


class OrgFsRuntime(
    AnnounceInstrumentation,
    StateLifecycle,
    StateContent,
    RuntimeDirectory,
    RuntimeLifecycle,
    RuntimeCheckout,
    RuntimeSpacePurge,
):
    """Own all OrgFS resources of one daemon generation on the state store."""

    def __init__(
        self,
        state_dir: Path,
        *,
        node_id: str,
        author: str,
        actor: str | None = None,
        logger: Callable[..., None] | None = None,
        owner_notifier: Callable[[str, str, dict[str, object]], None] | None = None,
        excluded_orgs: Callable[[], frozenset[str]] | None = None,
        space_binding_state: Callable[[str], Mapping[str, object] | None] | None = None,
        announce_instrumentation_enabled: bool = False,
        trust_tailnet_members: bool = False,
        actor_host: ActorHost | None = None,
        device_owner_hint: Callable[[str], str | None] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.node_id = node_id
        self.author = author
        self.actor = actor
        self.logger = logger
        self._announce_instrumentation_enabled = announce_instrumentation_enabled
        self.owner_notifier = owner_notifier
        self._trust_tailnet_members = trust_tailnet_members
        self._device_owner_hint = device_owner_hint
        self._directory_index_keys: dict[str, tuple[bytes, bytes]] = {}
        self._catalog_retries: dict[str, tuple[int, float]] = {}
        self._join_locks: dict[str, _JoinLock] = {}
        self._join_locks_guard = Lock()
        self._excluded_orgs = excluded_orgs or (lambda: frozenset())
        self._space_binding_state = space_binding_state or (lambda _space_id: None)
        self._actor_hosts = OrgFsActorHostCustody(actor_host)
        self.blobs = BlobAuthority(self.state_dir, actor_host=self._actor_hosts.host)
        self.stores = StateStoreRegistry()
        self._spaces_root = self.state_dir / "orgfs" / "spaces"
        self.facade = LocalOrgFs(
            author=author,
            actor=actor,
            node_id=node_id,
            blobs=self.blobs,
            store_factory=self._open_store,
            store_discard=self._discard_space_directory,
            actor_host=self._actor_hosts.host,
            logger=logger,
        )
        self.facade.mesh = self
        self._resource_lock = RLock()
        self._closing = False
        self._closed = False
        self._closed_event = threading.Event()
        self._close_lock = Lock()
        self._checkouts: dict[str, CheckoutAuthority] = {}
        self._checkout_lock = Lock()
        self._checkout_config_lock = Lock()
        self._checkout_closing = False
        self._checkout_config = self.state_dir / "orgfs" / "checkouts.json"
        self._mesh_creations: dict[str, _MeshCreation] = {}
        self._purging_spaces: set[str] = set(self._pending_space_purge_ids())
        self._lifecycle_lock = Lock()
        self._lifecycle_authority: _LifecycleAuthority | None = None
        self._lifecycle_context = threading.local()
        self._lifecycle_closing = False
        try:
            self._open_spaces()
        except BaseException:
            self._abandon_construction()
            raise
        self._restore_checkouts()
        self._session: TransportSession | None = None
        self._supplier_online: Callable[[str], bool] = lambda _supplier: True
        self._holder_candidates: Callable[[], tuple[str, ...]] = lambda: ()
        self._supplier_device_owners: dict[str, dict[str, str]] = {}
        self._supplier_device_watches: dict[str, object] = {}
        self._supplier_device_lock = Lock()
        self._meshes: dict[str, Any] = {}
        self._supplier_sync_health = SupplierSyncHealth()
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
        self._edge_skipped_holdings: set[tuple[str, str]] = set()
        self._pending_announces_lock = Lock()
        self._announce_buffer_dropped = 0
        self._announce_buffer_log_bucket = -1
        self._directory_generation = 1
        self._directory_cell: ActorCell | None = None
        self._directory_effects: EffectLane[_DirectoryEffectBatch, None] | None = None
        self._directory_closing = False
        self._directory_dropped = 0
        self._directory_custody_lock = Lock()
        self._directory_pending: set[tuple[str, int]] = set()
        self._directory_rebuild_pending = False
        self._directory_sync_due: float | None = None
        self._directory_sync_offset = self._new_directory_sync_offset()
        self._directory_sync_last: dict[str, str] = {}
        self._directory_capacity = self._directory_mailbox_capacity()
        self._directory_deferred: list[EffectRequest[_DirectoryEffectBatch]] = []
        self._resume_space_purges()

    def _abandon_construction(self) -> None:
        """Release what a failed startup scan already opened."""

        self.facade.close_effects(timeout=CLOSE_TIMEOUT_SECONDS)
        for store in self.stores.snapshot():
            store.close()
        self.stores.clear()
        self.blobs.close(timeout=CLOSE_TIMEOUT_SECONDS)
        self._actor_hosts.shutdown(CLOSE_TIMEOUT_SECONDS)
        self._closed = True

    def _open_store(self, space_id: str, create: bool) -> FileStateStore:
        store = FileStateStore.open(
            self._spaces_root / space_id, create=create, logger=self.logger
        )
        return self.stores.record(space_id, store)

    def _discard_space_directory(self, space_id: str) -> None:
        """Remove an unconfirmed space directory through the purge fence (§9 R6)."""

        store = self.stores.detach(space_id)
        if store is not None:
            store.close()
        space_fence, replica_fence = self._space_storage_fences(space_id)
        try:
            self._remove_fenced_tree(space_fence, space_id)
        finally:
            replica_fence.close()
            space_fence.close()

    def _open_spaces(self) -> None:
        for entry in classify_spaces(self._spaces_root, self._purging_spaces):
            space_id = entry.path.name
            if entry.verdict == "skip":
                continue
            if entry.verdict == "invalid-name":
                raise StoreError(
                    ipc_errors.ORGFS_INVALID_SPACE_DIRECTORY,
                    f"unexpected directory under {self._spaces_root}: {entry.path}; "
                    "space directories must be UUIDs",
                )
            if entry.verdict == "discard":
                self._discard_unfinished(space_id, entry.reason or "uninitialized")
                continue
            try:
                self.facade.load_space(space_id)
            except StoreError as exc:
                if exc.code != ipc_errors.ORGFS_LAYOUT_UNSUPPORTED:
                    raise
                self.stores.detach(space_id)
                self.facade.refuse_space(
                    space_id, exc.code, {**exc.details, "message": exc.message}
                )
                self._log(
                    "warn",
                    "orgfs.space.layout-unsupported",
                    spaceId=space_id,
                    reason=exc.details.get("reason"),
                )
            except OrgFsError as exc:
                if exc.code != "unknown-space":
                    raise
                self._discard_unfinished(space_id, "meta-without-space-root")

    def _discard_unfinished(self, space_id: str, reason: str) -> None:
        self._discard_space_directory(space_id)
        self._log("warn", "orgfs.space.unfinished-discarded", spaceId=space_id, reason=reason)

    def close(self) -> None:
        with self._close_lock:
            with self._resource_lock:
                if self._closed:
                    return
                self._closing = True
            self._closed_event.set()
            self._close_impl()

    def _close_impl(self) -> None:
        for stop_name in ("_stop_rebuild_complete", "_stop_supplier_online"):
            stop = getattr(self, stop_name)
            if stop is not None:
                stop()
                setattr(self, stop_name, None)
        with self._resource_lock:
            self._closing = True
            creations = tuple(self._mesh_creations.values())
        self._closed_event.set()
        deadline = time.monotonic() + CLOSE_TIMEOUT_SECONDS
        for creation in creations:
            if not creation.done.wait(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("orgfs sync creation did not drain")
        if not self._close_lifecycle(timeout=CLOSE_TIMEOUT_SECONDS):
            raise TimeoutError("orgfs directory lifecycle did not drain")
        with self._checkout_lock:
            self._checkout_closing = True
            checkouts = tuple(self._checkouts.values())
        for checkout in checkouts:
            if isinstance(checkout, CheckoutAuthority) and not checkout.close(timeout=CLOSE_TIMEOUT_SECONDS):
                raise TimeoutError("orgfs checkout I/O did not drain")
        with self._checkout_lock:
            self._checkouts.clear()
        self._withdraw_announcement()
        for name in ("_announce_registration", "_liveliness_registration"):
            registration = getattr(self, name)
            if registration is not None:
                registration.close()
                setattr(self, name, None)
        with self._supplier_device_lock:
            watches = tuple(self._supplier_device_watches.values())
            self._supplier_device_watches.clear()
            self._supplier_device_owners.clear()
        for watch in watches:
            close = getattr(watch, "close", None)
            if callable(close):
                close()
        if not self._close_directory_ingress(timeout=CLOSE_TIMEOUT_SECONDS):
            raise TimeoutError("orgfs directory effects did not drain")
        with self._resource_lock:
            syncs = tuple(self._meshes.items())
        if not self.facade.drain_broadcasts(tuple(space_id for space_id, _sync in syncs), timeout=CLOSE_TIMEOUT_SECONDS):
            raise TimeoutError("orgfs commit broadcasts did not drain")
        for space_id, sync in reversed(syncs):
            sync.close(timeout=CLOSE_TIMEOUT_SECONDS)
            self.facade.detach_sync(space_id)
        if not self.facade.close_effects(timeout=CLOSE_TIMEOUT_SECONDS):
            raise TimeoutError("orgfs post-commit effects did not drain")
        with self._resource_lock:
            self._meshes.clear()
        with self._pending_announces_lock:
            self._pending_announces.clear()
            self._lively_peers.clear()
        with self._resource_lock:
            self._session = None
        if not self.blobs.close(timeout=CLOSE_TIMEOUT_SECONDS):
            raise TimeoutError("orgfs blob authority did not drain")
        if not self._actor_hosts.shutdown(CLOSE_TIMEOUT_SECONDS):
            raise TimeoutError("orgfs actor host did not drain")
        for store in self.stores.snapshot():
            store.close()
        self.stores.clear()
        with self._resource_lock:
            self._closed = True

    def _withdraw_announcement(self) -> None:
        if self._session is None:
            return
        payload = {
            "schemaVersion": 1,
            "type": "orgfs-announce",
            "protocol": 3,
            "node": self.node_id,
            "durable": True,
            "spaces": [],
        }
        try:
            self._session.put(
                KeySpace().orgfs_announce(self.node_id),
                json.dumps(payload, separators=(",", ":")).encode(),
            )
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = ["OrgFsRuntime"]
