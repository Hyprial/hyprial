"""Daemon-owned composition for the P1 orgfs store, mesh, and local facade."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from threading import Lock
import time
from typing import Any, Final

from hyprial.transport import KeySpace, Registration, TransportSample, TransportSession

from .api import OrgFsError, SpaceInfo, SpaceStatus
from .blobs import BlobStore
from .checkout import CheckoutManager
from .docs import LocalOrgFs
from .mesh import (
    ORGFS_ANNOUNCE_BUFFER_LIMIT,
    ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT,
    OrgFsMesh,
)
from .replica import FsReplicaBackend, MemoryReplicaBackend, ReplicaStore
from .store import LocalSpaceStore, StoreError, state_covers


ORGFS_CONTENT_WAIT_S: Final[float] = 10.0


class _StoreRegistry(dict[str, LocalSpaceStore]):
    def __init__(self, runtime: "OrgFsRuntime") -> None:
        super().__init__()
        self.runtime = runtime

    def get(self, key: str, default: Any = None) -> LocalSpaceStore:
        if key not in self:
            self[key] = LocalSpaceStore(
                self.runtime.state_dir,
                key,
                node_id=self.runtime.node_id,
                blob_store=self.runtime.blobs,
            )
        return self[key]


#: Backoff between await_content provisioning attempts for one blob.
ORGFS_AWAIT_RETRY_SECONDS = 0.1


class OrgFsRuntime:
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
        self.blobs = BlobStore(self.state_dir)
        self.stores = _StoreRegistry(self)
        self.facade = LocalOrgFs(
            self.stores,
            self.blobs,
            self,
            author=author,
            actor=actor,
            node_id=node_id,
        )
        self._checkouts: dict[str, CheckoutManager] = {}
        self._checkout_registrations: dict[str, Any] = {}
        self._checkout_config = self.state_dir / "orgfs" / "checkouts.json"
        self._replicas: dict[str, ReplicaStore] = {}
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
        self._holders: dict[str, set[str]] = {}
        self._known_holders: dict[str, set[str]] = {}
        self._lively_peers: set[str] = set()
        self._pending_announces: dict[str, dict[str, Any]] = {}
        self._pending_announces_lock = Lock()
        self._announce_buffer_dropped = 0
        self._announce_buffer_log_bucket = -1

    def bind_transport(
        self,
        session: TransportSession,
        *,
        supplier_online: Callable[[str], bool] | None = None,
        holder_candidates: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._session = session
        if supplier_online is not None:
            self._supplier_online = supplier_online
        if holder_candidates is not None:
            self._holder_candidates = holder_candidates
        self._announce_registration = session.subscribe(
            f"{KeySpace().prefix}/org/fs/announce/*", self._on_announce
        )
        observe_liveliness = getattr(session, "observe_liveliness", None)
        if callable(observe_liveliness):
            self._liveliness_registration = observe_liveliness(
                f"{KeySpace().prefix}/liveliness/actor/*",
                self._on_peer_liveliness,
                history=True,
            )
        for space_id in tuple(self.stores):
            self._mesh(space_id)

    def _on_peer_liveliness(self, sample: TransportSample) -> None:
        """F5/F6: admit buffered discovery and schedule bounded anti-entropy."""

        peer, pending = self._settle_peer_liveliness(sample)
        if not peer or sample.kind == "delete":
            return
        self._announce_all()
        if pending is not None:
            self._apply_announce(peer, pending)
        # An announce can race the first pop while the liveliness callback
        # is applying the previous buffered value.  Recheck once after the
        # presence verdict has flipped so that value cannot be stranded
        # until a later liveliness transition.
        with self._pending_announces_lock:
            landed_during_flip = self._pending_announces.pop(peer, None)
        if landed_during_flip is not None:
            self._apply_announce(peer, landed_during_flip)
        for space_id, checkout in tuple(self._checkouts.items()):
            try:
                checkout.reconcile()
            except Exception as exc:  # noqa: BLE001 - holder retry is best effort
                if self.logger is not None:
                    self.logger(
                        "warn",
                        "orgfs.checkout.reconcile-failed",
                        spaceId=space_id,
                        detail=str(exc),
                    )
        for space_id in tuple(self.stores):
            # F6: only schedule anti-entropy for spaces the peer actually
            # announced when holder hints exist.  A freshly restarted
            # node has no in-memory hints yet, so it retains the P1
            # liveliness fallback until the first announce arrives.
            holders = self._holders.get(space_id, set())
            if holders and peer not in holders:
                continue
            mesh = self._mesh(space_id)
            if mesh is not None:
                mesh.schedule_sync_from(peer)

    def _settle_peer_liveliness(
        self, sample: TransportSample
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Track host presence and settle holder hints on liveliness delete."""

        peer = self._host_peer_from_liveliness(sample)
        if not peer or peer == self.node_id:
            return None, None
        with self._pending_announces_lock:
            if sample.kind == "delete":
                self._lively_peers.discard(peer)
                for holders in self._holders.values():
                    holders.discard(peer)
                return peer, None
            self._lively_peers.add(peer)
            return peer, self._pending_announces.pop(peer, None)

    @staticmethod
    def _host_peer_from_liveliness(sample: TransportSample) -> str | None:
        prefix = f"{KeySpace().prefix}/liveliness/actor/"
        if not sample.key.startswith(prefix):
            return None
        peer = KeySpace().decode_identity(sample.key[len(prefix) :])
        # Import here to avoid making the orgfs module initialize the daemon
        # package while DaemonApplication is importing OrgFsRuntime.
        from hyprial.daemon.identity import classify_target_identity
        from hyprial.uri import TARGET_KIND_HOST

        if classify_target_identity(peer) != TARGET_KIND_HOST:
            return None
        return peer

    def _announcement_spaces(self) -> tuple[dict[str, object], ...]:
        spaces: list[dict[str, object]] = []
        for info in sorted(self.facade.spaces(), key=lambda item: item.space_id):
            mode = next(
                (
                    member.mode
                    for member in self.facade.members(info.space_id)
                    if member.user == self.author
                ),
                None,
            )
            if mode is None:
                continue
            roles = ["member"]
            if info.space_id in self._replicas:
                roles.append("resident")
            spaces.append({"spaceId": info.space_id, "roles": roles, "mode": mode})
        return tuple(spaces)

    def _announce_all(self) -> None:
        if self._session is None or not self._meshes:
            return
        next(iter(self._meshes.values())).announce()

    def _on_announce(self, sample: TransportSample) -> None:
        try:
            value = json.loads(sample.payload)
            prefix = f"{KeySpace().prefix}/org/fs/announce/"
            if not sample.key.startswith(prefix):
                return
            key_node = sample.key[len(prefix) :]
            if (
                not isinstance(value, dict)
                or value.get("schemaVersion") != 1
                or value.get("type") != "orgfs-announce"
                or value.get("node") != key_node
                or not key_node
                or "/" in key_node
                or not isinstance(value.get("spaces"), list)
            ):
                return
            if not self._supplier_online(key_node):
                self._buffer_announce(key_node, value)
                return
            with self._pending_announces_lock:
                self._pending_announces.pop(key_node, None)
            self._apply_announce(key_node, value)
        except Exception:
            return

    def _buffer_announce(self, node: str, value: dict[str, Any]) -> None:
        with self._pending_announces_lock:
            spaces = value["spaces"]
            previous = self._pending_announces.get(node)
            if (
                previous is None
                and len(self._pending_announces) >= ORGFS_ANNOUNCE_BUFFER_LIMIT
            ):
                evicted = next(iter(self._pending_announces))
                self._pending_announces.pop(evicted, None)
                self._record_announce_buffer_drop(
                    reason="announcer-limit",
                    node=node,
                    dropped=1,
                )
            if not spaces or previous is None or not previous["spaces"]:
                retained = list(spaces[:ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT])
                dropped = len(spaces) - len(retained)
                self._pending_announces[node] = {**value, "spaces": retained}
                if dropped:
                    self._record_announce_buffer_drop(
                        reason="space-limit",
                        node=node,
                        dropped=dropped,
                        space_count=len(retained),
                    )
                return
            by_space = {
                str(space["spaceId"]): space
                for space in previous["spaces"]
                if isinstance(space, dict) and isinstance(space.get("spaceId"), str)
            }
            by_space.update(
                {
                    str(space["spaceId"]): space
                    for space in spaces
                    if isinstance(space, dict) and isinstance(space.get("spaceId"), str)
                }
            )
            merged = list(by_space.values())
            retained = merged[:ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT]
            self._pending_announces[node] = {**value, "spaces": retained}
            dropped = len(merged) - len(retained)
            if dropped:
                self._record_announce_buffer_drop(
                    reason="space-limit",
                    node=node,
                    dropped=dropped,
                    space_count=len(retained),
                )

    def _record_announce_buffer_drop(
        self,
        *,
        reason: str,
        node: str,
        dropped: int,
        space_count: int = 0,
    ) -> None:
        self._announce_buffer_dropped += dropped
        bucket = self._announce_buffer_dropped.bit_length()
        if bucket == self._announce_buffer_log_bucket:
            return
        self._announce_buffer_log_bucket = bucket
        if self.logger is not None:
            self.logger(
                "warn",
                "orgfs.announce.buffer-dropped",
                reason=reason,
                node=node,
                droppedCount=self._announce_buffer_dropped,
                announcerCount=len(self._pending_announces),
                announcerLimit=ORGFS_ANNOUNCE_BUFFER_LIMIT,
                spaceCount=space_count,
                spacesPerEntryLimit=ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT,
            )

    def _apply_announce(self, node: str, value: dict[str, Any]) -> None:
        spaces = value["spaces"]
        if not spaces:
            for holders in self._holders.values():
                holders.discard(node)
            return
        for space in spaces:
            if isinstance(space, dict) and isinstance(space.get("spaceId"), str):
                space_id = str(space["spaceId"])
                self._holders.setdefault(space_id, set()).add(node)
                self._known_holders.setdefault(space_id, set()).add(node)

    def _mesh(self, space_id: str) -> OrgFsMesh | None:
        if self._session is None:
            return None
        if space_id not in self._meshes:

            def applied(envelope: bytes) -> tuple[str, ...]:
                self.facade.apply_envelope(space_id, envelope)
                self._reconcile_retirements(space_id)
                return self.facade.retained_blob_digests(space_id)

            def replacement(
                old_doc_id: str, new_doc_id: str, snapshot_bytes: bytes
            ) -> None:
                if space_id in self.facade._spaces:  # noqa: SLF001
                    self.facade.install_replacement_snapshot(
                        space_id, old_doc_id, new_doc_id, snapshot_bytes
                    )
                    self._reconcile_retirements(space_id)

            mesh = OrgFsMesh(
                self._session,
                self.stores.get(space_id),
                self.node_id,
                author=self.author,
                blob_store=self.blobs,
                supplier_online=self._supplier_online,
                announcement_source=self._announcement_spaces,
                holder_discovery=self._holders.setdefault(space_id, set()),
                recovery_candidates=lambda: (
                    *self._holders.get(space_id, ()),
                    *self._holder_candidates(),
                ),
                on_applied=applied,
                on_replacement=replacement,
                logger=self.logger,
            )
            self._meshes[space_id] = mesh
            replica = self._replicas.get(space_id)
            if replica is not None:
                mesh.attach_replica(replica)
            self._announce_all()
        return self._meshes[space_id]

    def _reconcile_retirements(self, space_id: str) -> None:
        """Finish G2 cleanup and ack after all replacement bytes are local."""

        store = self.stores.get(space_id)
        records = store.retirement_records()
        if store.pending_replacements():
            return
        replica = self._replicas.get(space_id)
        purge_entries = store.purge_list_entries()
        retirement_plan_ids = {record.plan_id for record in records}
        for record in records:
            snapshot = store.snapshot(record.replacement_doc_id, shallow_since=None)
            if replica is not None:
                plan_blobs = tuple(
                    {"sha": digest}
                    for digest, plan_id in sorted(purge_entries.items())
                    if plan_id == record.plan_id
                )
                replica.apply_retirement(
                    {
                        "oldPhysicalDocId": record.old_physical_doc_id,
                        "replacementDocId": record.replacement_doc_id,
                        "snapshotId": record.snapshot_id,
                        "replacementSnapshotBytes": snapshot.snapshot_bytes,
                    },
                    {
                        # R4: this replicated purge-list inventory is owner-authored
                        # and survives replica log retention.  Reconstructing the
                        # delete set from remaining rows loses trimmed updateBlob
                        # digests and can acknowledge with forbidden residue.
                        "blobs": plan_blobs,
                    },
                )
            store.delete_retired_objects(record.old_physical_doc_id)
            if store.retired_residue(record.old_physical_doc_id):
                raise StoreError("invalid-argument", "retired document residue remains")

        if replica is not None:
            replica.apply_blob_purge(
                digest
                for digest, plan_id in purge_entries.items()
                if plan_id not in retirement_plan_ids
            )
            for doc_id in store.document_ids():
                if store.snapshot_point(doc_id) is not None:
                    replica.apply_retention(doc_id)
        for digest in purge_entries:
            self.blobs.release(space_id, digest)
            self.blobs.delete_if_unreferenced(digest)

        plans = {record.plan_id for record in records} | set(purge_entries.values())
        for plan_id in sorted(plans):
            if any(
                record.plan_id == plan_id
                and store.retired_residue(record.old_physical_doc_id)
                for record in records
            ):
                continue
            self.facade.acknowledge_purge(space_id, plan_id)

    def broadcast_pending(self, space_id: str) -> int:
        mesh = self._mesh(space_id)
        return 0 if mesh is None else mesh.broadcast_pending()

    def reconcile_replica_blobs(self, space_id: str) -> tuple[str, ...]:
        """Reconcile retained local blob refs into the serving replica."""

        replica = self._replicas.get(space_id)
        if replica is None:
            return ()
        reconciled = replica.reconcile_blobs(self.blobs)
        for digest in reconciled:
            self.blobs.pin(space_id, digest, "replica")
        return reconciled

    def status(self, space_id: str) -> SpaceStatus:
        """Live holder view: durable probe history plus current presence.

        ``holders_online`` is the sorted, de-duplicated union of this node
        when it currently serves the space and the announced holders that
        are online now per the runtime's liveliness and presence view.
        ``durable_holders_seen`` keeps its probe-recorded meaning.
        """

        base = self.facade.status(space_id)
        with self._pending_announces_lock:
            announced = set(self._holders.get(space_id, ()))
            lively = set(self._lively_peers)
        # When this runtime observes host liveliness, presence is exactly that
        # view: an announce applied after a peer's liveliness delete must not
        # make it look online again.  The supplier predicate is only the
        # fallback for sessions without a liveliness view.
        observes_liveliness = self._liveliness_registration is not None
        online = {
            node
            for node in announced
            if node != self.node_id
            and (
                node in lively
                if observes_liveliness
                else self._supplier_online(node)
            )
        }
        if space_id in self._replicas:
            online.add(self.node_id)
        return SpaceStatus(
            base.space_id,
            base.unconfirmed_commits,
            base.durable_holders_seen,
            tuple(sorted(online)),
        )

    def writer_attributions(self, space_id: str) -> dict[str, tuple[str, ...]]:
        """Return the space's replicated node -> author attribution table."""

        if space_id not in {info.space_id for info in self.facade.spaces()}:
            raise OrgFsError("unknown-space", {"spaceId": space_id})
        return self.stores.get(space_id).writer_attributions()

    def refresh_space(self, space_id: str, *, timeout: float) -> bool:
        """Try every online holder once without exceeding the caller's bound."""

        if self._session is None:
            return False
        holders = tuple(
            sorted(
                node
                for node in self._holders.get(space_id, ())
                if node != self.node_id and self._supplier_online(node)
            )
        )
        if not holders:
            return True
        mesh = self._mesh(space_id)
        if mesh is None:
            return False
        deadline = time.monotonic() + timeout
        refreshed = True
        for holder in holders:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                mesh.sync_from(
                    holder,
                    timeout=remaining,
                    deadline_monotonic=deadline,
                )
            except (StoreError, OrgFsError):
                refreshed = False
            if time.monotonic() > deadline:
                refreshed = False
                break
        return refreshed

    def known_holders(
        self,
        space_id: str,
        *,
        doc_id: str | None = None,
        required_frontier: bytes | None = None,
    ) -> tuple[str, ...]:
        """Return in-memory holder hints, preferring proven doc coverage."""

        online = {
            node
            for node in self._holders.get(space_id, ())
            if node != self.node_id and self._supplier_online(node)
        }
        known = {
            node
            for node in self._known_holders.get(space_id, ())
            if node != self.node_id
        }
        candidates = online | known
        frontiers = self.stores.get(space_id).holder_frontiers()

        def key(node: str) -> tuple[bool, bool, str]:
            covers = False
            if doc_id is not None and required_frontier is not None:
                actual = frontiers.get(node, {}).get(doc_id)
                covers = actual is not None and state_covers(
                    actual, required_frontier
                )
            return (not covers, node not in online, node)

        return tuple(sorted(candidates, key=key))

    def await_content(
        self, space_id: str, node_id: str, *, deadline_monotonic: float
    ) -> bool:
        """Actively provision one node's content without holding the facade lock."""

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return False
        info = self.facade.stat(space_id, f"id:{node_id}")
        if info.kind == "blob":
            if info.content_state == "arrived":
                return True
            if not info.blob_hash:
                return False
            mesh = self._mesh(space_id)
            if mesh is None:
                return False
            # request_blob's event only means "this attempt ended"; a holder
            # that is not routable yet makes an attempt end without the blob.
            # Keep provisioning until the caller's deadline, with a short
            # backoff so an already-set event cannot spin.
            while True:
                event = mesh.request_blob(info.blob_hash)
                event.wait(max(0.0, deadline_monotonic - time.monotonic()))
                if self.blobs.contains(info.blob_hash):
                    return True
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(ORGFS_AWAIT_RETRY_SECONDS, remaining))
        if info.kind == "doc":
            if info.content_state in {"arrived", "unverifiable"}:
                return True
            self.refresh_space(
                space_id,
                timeout=max(0.0, deadline_monotonic - time.monotonic()),
            )
            refreshed = self.facade.stat(space_id, f"id:{node_id}")
            if refreshed.doc_id:
                snapshot = self.stores.get(space_id).snapshot(
                    refreshed.doc_id, shallow_since=None
                )
                self.facade.hydrate_content_snapshot(
                    space_id,
                    f"id:{node_id}",
                    snapshot.snapshot_bytes,
                    expected_doc_id=snapshot.doc_id,
                )
            return (
                self.facade.stat(space_id, f"id:{node_id}").content_state
                == "arrived"
            )
        return True

    def fetch_blob(self, space_id: str, digest: str) -> bytes:
        """Fetch a facade-referenced blob from any currently online holder."""

        candidates = sorted(
            {
                *(
                    node
                    for node in self._holders.get(space_id, ())
                    if node != self.node_id
                ),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        mesh = self._mesh(space_id)
        last_error: Exception | None = None
        if mesh is not None:
            for holder in candidates:
                if not self._supplier_online(holder):
                    continue
                try:
                    payload = mesh.fetch_blob(holder, digest)
                    stored = self.blobs.put(space_id, payload, reason="replica")
                    if stored != digest:
                        raise StoreError(
                            "blob-unavailable",
                            "fetched blob did not match requested digest",
                        )
                    replica = self._replicas.get(space_id)
                    if replica is not None:
                        replica.store_blob(digest, payload)
                        self.blobs.pin(space_id, digest, "replica")
                    return payload
                except StoreError as exc:
                    last_error = exc
        details: dict[str, object] = {
            "digest": digest,
            "lastKnownHolders": sorted(self._known_holders.get(space_id, ())),
        }
        if last_error is not None:
            details["message"] = str(last_error)
        raise OrgFsError("blob-unavailable", details)

    def join(self, space_id: str) -> SpaceInfo:
        holders = sorted(
            {
                *(
                    node
                    for node in self._holders.get(space_id, ())
                    if node != self.node_id
                ),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        if not holders:
            raise OrgFsError("no-holder-online", {"spaceId": space_id})
        mesh = self._mesh(space_id)
        assert mesh is not None
        last_error: Exception | None = None
        for holder in holders:
            try:
                mesh.sync_from(holder)
                info = self.facade.load_space(space_id)
                self._reconcile_retirements(space_id)
                self._announce_all()
                return info
            except (StoreError, OrgFsError) as exc:
                last_error = exc
        if last_error is not None:
            code = getattr(last_error, "code", "no-holder-online")
            raise OrgFsError(str(code), {"message": str(last_error)}) from last_error
        raise OrgFsError("no-holder-online", {"spaceId": space_id})

    def watch_events(
        self, space_id: str, glob: str, since_version: str | None
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "spaceId": event.space_id,
                "kind": event.kind,
                "node": event.node,
                "oldPath": event.old_path,
            }
            for event in self.facade.changes_since(space_id, glob, since_version)
        )

    def serve(self, space_id: str, backend: str = "fs") -> dict[str, object]:
        if backend not in {"fs", "memory"}:
            raise OrgFsError(
                "invalid-argument", {"message": "backend must be fs or memory"}
            )
        try:
            self.facade.stat(space_id, "id:root")
        except OrgFsError as exc:
            if exc.code != "unknown-space":
                raise
            self.join(space_id)
        local_store = self.stores.get(space_id)
        existing = self._replicas.get(space_id)
        if existing is not None:
            existing_backend = (
                "fs" if isinstance(existing.backend, FsReplicaBackend) else "memory"
            )
            if existing_backend != backend:
                raise OrgFsError(
                    "invalid-argument",
                    {
                        "message": "space is already served by another backend",
                        "spaceId": space_id,
                        "backend": existing_backend,
                    },
                )
            existing.reconcile_blobs(self.blobs)
            for digest in existing.pinned_blobs():
                self.blobs.pin(space_id, digest, "replica")
            self._reconcile_retirements(space_id)
            return {
                "spaceId": space_id,
                "backend": backend,
                "durable": existing.durable(),
                "pinnedBlobs": list(existing.pinned_blobs()),
            }
        replica_backend = (
            FsReplicaBackend(space_id, self.state_dir)
            if backend == "fs"
            else MemoryReplicaBackend(space_id)
        )

        def conflict_event(event: str, details: Any) -> None:
            if self.logger is not None:
                self.logger("error", f"orgfs.{event}", **dict(details))

        def owner_notice(event: str, details: Any) -> None:
            owner = next(
                info.owner for info in self.facade.spaces() if info.space_id == space_id
            )
            if self.logger is not None:
                self.logger(
                    "warn",
                    "orgfs.owner-notice",
                    notice=event,
                    owner=owner,
                    **dict(details),
                )
            if self.owner_notifier is not None:
                self.owner_notifier(owner, event, dict(details))

        replica = ReplicaStore(
            space_id,
            replica_backend,
            local_store,
            event_callback=conflict_event,
            owner_notifier=owner_notice,
        )
        replica.prime_from(local_store, self.blobs)
        for digest in replica.pinned_blobs():
            self.blobs.pin(space_id, digest, "replica")
        self._replicas[space_id] = replica
        mesh = self._mesh(space_id)
        if mesh is not None:
            mesh.attach_replica(replica)
        self._reconcile_retirements(space_id)
        return {
            "spaceId": space_id,
            "backend": backend,
            "durable": replica.durable(),
            "pinnedBlobs": list(replica.pinned_blobs()),
        }

    def purge_plan(self, space_id: str, targets: list[dict[str, Any]]) -> Any:
        return self.facade.purge_plan(space_id, targets)

    def purge(self, space_id: str, plan_id: str) -> Any:
        result = self.facade.purge(space_id, plan_id)
        self._reconcile_retirements(space_id)
        return result

    def purge_status(self, space_id: str, plan_id: str) -> Any:
        return self.facade.purge_status(space_id, plan_id)

    def unban(self, space_id: str, sha: str) -> None:
        self.facade.unban(space_id, sha)

    def _write_checkout_config(self) -> None:
        self._checkout_config.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._checkout_config.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"schemaVersion": 1, "spaces": sorted(self._checkouts)},
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._checkout_config)

    def _restore_checkouts(self) -> None:
        try:
            value = json.loads(self._checkout_config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            if self.logger is not None:
                self.logger("warn", "orgfs.checkout.restore-failed", detail=str(exc))
            return
        spaces = value.get("spaces") if isinstance(value, dict) else None
        if not isinstance(spaces, list):
            return
        for space_id in spaces:
            if not isinstance(space_id, str) or space_id not in self.stores:
                continue
            try:
                self._set_checkout(space_id, True, persist=False)
            except Exception as exc:
                if self.logger is not None:
                    self.logger(
                        "warn",
                        "orgfs.checkout.restore-failed",
                        spaceId=space_id,
                        detail=str(exc),
                    )

    def _set_checkout(
        self, space_id: str, enabled: bool, *, persist: bool
    ) -> dict[str, object]:
        # Validate membership and hydrate before touching a projection path.
        self.facade.stat(space_id, "id:root")
        root = self.state_dir / "orgfs" / "spaces" / space_id / "checkout"
        if enabled:
            manager = self._checkouts.get(space_id)
            if manager is None:
                manager = CheckoutManager(self.facade, self.blobs, space_id, root)
                manager.materialize()
                registration = self.facade.watch(space_id, "*", manager.apply)
                self._checkouts[space_id] = manager
                self._checkout_registrations[space_id] = registration
            else:
                manager.materialize()
        else:
            registration = self._checkout_registrations.pop(space_id, None)
            if registration is not None:
                registration.close()
            manager = self._checkouts.pop(space_id, None)
            if manager is None:
                manager = CheckoutManager(self.facade, self.blobs, space_id, root)
            manager.disable()
        if persist:
            self._write_checkout_config()
        return {"spaceId": space_id, "enabled": enabled, "path": str(root)}

    def checkout(self, space_id: str, enabled: bool) -> dict[str, object]:
        return self._set_checkout(space_id, bool(enabled), persist=True)

    def close(self) -> None:
        for registration in tuple(self._checkout_registrations.values()):
            registration.close()
        self._checkout_registrations.clear()
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
        for mesh in reversed(tuple(self._meshes.values())):
            mesh.close()
        self._meshes.clear()
        self._replicas.clear()
        with self._pending_announces_lock:
            self._pending_announces.clear()
            self._lively_peers.clear()
        self._session = None
        for store in tuple(self.stores.values()):
            store.close()
        self.stores.clear()
        self.blobs.close()


__all__ = ["OrgFsRuntime"]
