# Decision record (C3, 2026-11-25, design §4.4, §4.7):
# - Threading.  The journal mesh admits broadcasts into a kernel actor cell
#   whose effect lane (one worker) applies them, and runs sync and blob jobs
#   on a separate single-thread executor (mesh.py:184-232, ingress.py:107-160).
#   This package keeps the same shape with plain primitives for testability:
#   a bounded credit counter (capacity 128) in front of a single-thread ingress
#   executor, and a single-thread worker executor for rounds and rebuild
#   re-declaration.  Overflow drops the broadcast and counts it; the next pull
#   recovers it.  Swapping in the actor cell / effect lane is a phase D choice.
# - SupplierSyncMixin (network/supplier_health.py) is inherited unchanged, so
#   schedule_sync_from's coalescing, queue limit (32), shared/local holds and
#   retry ladder are the journal mesh's.  The mixin reads ``self.store.
#   space_id`` for its log fields; ``store`` is a read-only value carrying the
#   space id, not a store.
# - Node gate (§3.5) = presence callback AND SyncHost.supplier_allowed.  The
#   presence half is what the mixin calls _supplier_allowed (an offline peer is
#   a global failure); a host refusal is raised inside the round with reason
#   "supplier-not-member", which the mixin treats as space-local, no retry.
# - close(timeout) follows mesh.close: stop admitting, close registrations,
#   drain admitted broadcasts and in-flight query handlers, then wait for
#   accepted worker jobs, all against one deadline, raising TimeoutError when
#   it passes.  Rounds check the closed flag before every host call, so once
#   close returns without raising, nothing calls the SyncHost again.
# - Rebuild: when the session offers on_rebuild_complete, the hook only queues
#   a re-declaration on the worker (the hook must not block); the worker closes
#   the old registrations and declares new ones.
# - Candidate peers for read-first requests, maintain() and request_blob are
#   holder_discovery() (optional, §4.7 revision) followed by peers this
#   instance has synced with or received an admitted broadcast from (bounded,
#   most recent first), each filtered by presence.
# - announce() publishes announcement_source() when given (node-level list,
#   same shape and oversize fallback to bare space ids as the journal mesh),
#   otherwise this space alone; both carry "protocol": 3.
# - Package F6 (real-node finding R3-1): broadcasts reach every subscriber,
#   so the receiver enforces "a removed member holds meta only".  While
#   SyncHost.local_member() is false, a broadcast that passed the node gate is
#   applied only when it is the meta document; tree and content frames are
#   dropped (logged at debug, not counted against the sender), and no content
#   is requested.  A meta update that makes this node a member again schedules
#   a pull from its sender so the space catches up without waiting for a tick.
# - Package F6 (R3-2): a broadcast the receive gate refuses because the
#   sender's device owner is not a current member is counted in
#   ``rejected_updates``, which the coordinator adds to the space status.
#   Offline senders and undecodable frames are logged, not counted.
# - Package F7 (real-node finding R4-3, rulings 3 and 4): the node gate runs
#   before a supplier is chosen.  ``schedule_sync_from`` returns False for a
#   node whose device owner is not a current member, before any admission,
#   so such a node is never queued, never fails a round and never enters the
#   shared supplier backoff; ``_candidate_peers`` and ``observations()`` skip
#   it the same way, so a removed member's observation takes no part in
#   ``synced``, "behind" or "ahead".  The gate is read at every call, so a
#   re-admitted member's node is a candidate again at once.
# - Package F7 (ruling 2): the F6 re-admission pull now goes to the sender
#   and every known member peer of the space, and an accepted meta update
#   that re-admits another member's known node pulls from it
#   (``_meta_accepted``; the rule is written out in ``sync/pull.py``).
"""StateSpaceSync: the protocol 3 SpaceSync implementation for one space."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Condition, Event, Lock
import time
from typing import Callable

from hyprial.daemon.impl.orgfs.network.supplier_health import (
    SupplierSyncHealth,
    SupplierSyncMixin,
)
from hyprial.daemon.impl.orgfs.storage.blobs import BlobStore
from hyprial.daemon.impl.orgfs.sync.blobs import SyncBlobMixin, blob_wildcard
from hyprial.daemon.impl.orgfs.sync.frames import (
    UpdateFrame,
    decode_update,
    encode_update,
    json_bytes,
)
from hyprial.daemon.impl.orgfs.sync.interface import (
    EMPTY_VECTOR,
    SYNC_CONTENT_DOCUMENT_LIMIT,
    SYNC_ENVELOPE_BYTES,
    SYNC_INGRESS_CAPACITY,
    SYNC_PROTOCOL,
    AnnouncementSource,
    HolderDiscovery,
    PeerObservation,
    PresenceGate,
    SyncHost,
    SyncLogger,
    outcome_kind,
)
from hyprial.daemon.impl.orgfs.sync.pull import SyncPullMixin
from hyprial.daemon.impl.orgfs.sync.vectors import FrameError, covers
from hyprial.daemon.impl.transport import KeySpace, Registration, TransportSample, TransportSession

_KNOWN_PEER_LIMIT = 256


@dataclass(frozen=True, slots=True)
class _SpaceRef:
    space_id: str


class StateSpaceSync(SupplierSyncMixin, SyncPullMixin, SyncBlobMixin):
    """Pull, serve and broadcast one space's documents through a SyncHost."""

    def __init__(
        self,
        session: TransportSession,
        keys: KeySpace,
        space_id: str,
        node_id: str,
        author: str,
        host: SyncHost,
        blob_store: BlobStore | None,
        supplier_health: SupplierSyncHealth,
        presence: PresenceGate,
        logger: SyncLogger | None = None,
        *,
        announcement_source: AnnouncementSource | None = None,
        holder_discovery: HolderDiscovery | None = None,
    ) -> None:
        if not node_id:
            raise ValueError("node_id must not be empty")
        keys.orgfs3_sync(space_id, node_id)
        self.session = session
        self._keys = keys
        self.space_id = space_id
        self.node_id = node_id
        self.author = author
        self.host = host
        self.blob_store = blob_store
        self._presence = presence
        self._logger = logger
        self._announcement_source = announcement_source
        self._holder_discovery = holder_discovery
        self._blob_waiters: dict[str, list[Event]] = {}
        self._closed = False
        self._close_done = False
        self._worker_lock = Lock()
        self._sync_pending: set[str] = set()
        self._sync_active: set[str] = set()
        self._sync_rerun: set[str] = set()
        self._sync_dropped = 0
        self._supplier_sync_health = supplier_health
        self._supplier_sync_local_health = SupplierSyncHealth()
        self._supplier_sync_held_until_total = 0
        self._observations: dict[str, PeerObservation] = {}
        self._pull_backs: dict[str, tuple[bytes, bytes]] = {}
        self._priority: list[str] = []
        self._known_peers: dict[str, None] = {}
        self._dependency_retries: dict[str, int] = {}
        self._temporary_blobs: dict[str, float] = {}
        self._worker_futures: set[Future[object]] = set()
        self._worker_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"orgfs3-sync-{space_id[:12]}"
        )
        self._ingress_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"orgfs3-ingress-{space_id[:12]}"
        )
        self._ingress_guard = Condition(Lock())
        self._ingress_pending = 0
        self._ingress_dropped = 0
        self._rejected_updates = 0
        self._ingress_closing = False
        self._handlers_active = 0
        self._registrations: list[Registration] = []
        self._stop_rebuild: Callable[[], None] | None = None
        try:
            self._registrations = self._declare()
            hook = getattr(session, "on_rebuild_complete", None)
            if callable(hook):
                self._stop_rebuild = hook(self._rebuild_completed)
        except BaseException:
            self.close()
            raise

    @property
    def store(self) -> _SpaceRef:
        return _SpaceRef(self.space_id)

    @property
    def ingress_dropped(self) -> int:
        with self._ingress_guard:
            return self._ingress_dropped

    @property
    def rejected_updates(self) -> int:
        with self._ingress_guard:
            return self._rejected_updates

    def _local_member(self) -> bool:
        try:
            return bool(self.host.local_member())
        except Exception:  # noqa: BLE001 - an unreadable membership fails closed
            return False

    def idle(self) -> bool:
        """True when no broadcast, handler or worker job is outstanding."""

        with self._ingress_guard:
            ingress_idle = self._ingress_pending == 0 and self._handlers_active == 0
        with self._worker_lock:
            workers_idle = not any(not future.done() for future in self._worker_futures)
        return ingress_idle and workers_idle

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger(level, event, **fields)

    def _declare(self) -> list[Registration]:
        registrations: list[Registration] = []
        try:
            registrations.append(
                self.session.subscribe(self._keys.orgfs3_update(self.space_id), self._admit_update)
            )
            registrations.append(
                self.session.declare_query_handler(
                    self._keys.orgfs3_sync(self.space_id, self.node_id), self._handle_sync
                )
            )
            if self.blob_store is not None:
                registrations.append(
                    self.session.declare_query_handler(blob_wildcard(self._keys), self._handle_blob)
                )
        except BaseException:
            for registration in reversed(registrations):
                registration.close()
            raise
        return registrations

    def _rebuild_completed(self) -> None:
        self._submit_worker(lambda _key: self._redeclare(), "rebuild")

    def _redeclare(self) -> None:
        with self._worker_lock:
            if self._closed:
                return
            old, self._registrations = self._registrations, []
        for registration in reversed(old):
            try:
                registration.close()
            except Exception:  # noqa: BLE001 - a lost registration may already be gone
                pass
        try:
            fresh = self._declare()
        except Exception as exc:  # noqa: BLE001 - the next rebuild retries
            self._log("warn", "orgfs.sync.redeclare-failed", spaceId=self.space_id, detail=str(exc))
            return
        with self._worker_lock:
            closed = self._closed
            if not closed:
                self._registrations = fresh
        if closed:
            for registration in reversed(fresh):
                registration.close()
        else:
            self._log("info", "orgfs.sync.redeclared", spaceId=self.space_id)

    def _enter_handler(self) -> bool:
        with self._ingress_guard:
            if self._ingress_closing:
                return False
            self._handlers_active += 1
            return True

    def _leave_handler(self) -> None:
        with self._ingress_guard:
            self._handlers_active -= 1
            self._ingress_guard.notify_all()

    def _supplier_allowed(self, supplier: str) -> bool:
        try:
            return bool(supplier) and supplier != self.node_id and bool(self._presence(supplier))
        except Exception:  # noqa: BLE001 - a broken presence view fails closed
            return False

    def _remember_peer(self, peer: str) -> None:
        self._known_peers.pop(peer, None)
        self._known_peers[peer] = None
        while len(self._known_peers) > _KNOWN_PEER_LIMIT:
            self._known_peers.pop(next(iter(self._known_peers)))

    def _member_node(self, node: str) -> bool:
        try:
            return bool(self.host.supplier_allowed(node))
        except Exception:  # noqa: BLE001 - an unresolvable owner fails closed
            return False

    def schedule_sync_from(self, peer: str) -> bool:
        if not peer or peer == self.node_id or self._closed or not self._member_node(peer):
            return False
        return super().schedule_sync_from(peer)

    def _submit_worker(self, operation: Callable[[str], object], key: str) -> bool:
        with self._worker_lock:
            if self._closed:
                return False
            try:
                future = self._worker_executor.submit(operation, key)
            except RuntimeError:
                return False
            self._worker_futures.add(future)
        future.add_done_callback(self._worker_finished)
        return True

    def _worker_finished(self, future: Future[object]) -> None:
        with self._worker_lock:
            self._worker_futures.discard(future)

    def announce(self) -> None:
        if self._closed:
            return
        spaces = (
            [dict(space) for space in self._announcement_source()]
            if self._announcement_source is not None
            else [{"spaceId": self.space_id, "roles": ["member"], "mode": "rw"}]
        )
        header = {"schemaVersion": 1, "type": "orgfs-announce", "protocol": SYNC_PROTOCOL,
                  "node": self.node_id, "durable": True}
        payload = json_bytes({**header, "spaces": spaces})
        if len(payload) > SYNC_ENVELOPE_BYTES:
            payload = json_bytes({**header, "spaces": [
                {"spaceId": str(space["spaceId"])} for space in spaces
                if isinstance(space.get("spaceId"), str)
            ]})
        self.session.put(self._keys.orgfs_announce(self.node_id), payload)

    def publish(self, doc_id: str, update: bytes, vector: bytes) -> None:
        if self._closed:
            return
        payload = encode_update(UpdateFrame(self.space_id, self.node_id, doc_id, vector, update))
        try:
            self.session.put(self._keys.orgfs3_update(self.space_id), payload)
        except Exception as exc:  # noqa: BLE001 - the commit is durable; pulls recover
            self._log("warn", "orgfs.sync.publish-failed", spaceId=self.space_id,
                      docId=doc_id, detail=str(exc))

    def _admit_update(self, sample: TransportSample) -> None:
        payload = bytes(sample.payload)
        with self._ingress_guard:
            if (
                self._ingress_closing
                or self._ingress_pending >= SYNC_INGRESS_CAPACITY
                or len(payload) > SYNC_ENVELOPE_BYTES
            ):
                self._ingress_dropped += 1
                return
            self._ingress_pending += 1
            try:
                self._ingress_executor.submit(self._process_update, payload)
            except RuntimeError:
                self._ingress_pending -= 1
                self._ingress_dropped += 1

    def _process_update(self, payload: bytes) -> None:
        try:
            self._receive_update(payload)
        except Exception as exc:  # noqa: BLE001 - one bad frame never kills ingress
            self._log("warn", "orgfs.update.failed", spaceId=self.space_id, detail=str(exc))
        finally:
            with self._ingress_guard:
                self._ingress_pending -= 1
                self._ingress_guard.notify_all()

    def _receive_update(self, payload: bytes) -> None:
        try:
            frame = decode_update(payload, space_id=self.space_id)
        except FrameError as exc:
            self._log("warn", "orgfs.update.rejected", reason="invalid-argument",
                      detail=str(exc), peer="")
            return
        peer = frame.node
        if peer == self.node_id:
            return
        if not self._supplier_allowed(peer):
            self._log("warn", "orgfs.update.rejected", reason="supplier-offline", peer=peer)
            return
        if not self.host.supplier_allowed(peer):
            with self._ingress_guard:
                self._rejected_updates += 1
            self._log("warn", "orgfs.update.rejected", reason="supplier-not-member", peer=peer)
            return
        with self._worker_lock:
            self._remember_peer(peer)
        member = self._local_member()
        if not member and frame.doc_id != "meta":
            self._log("debug", "orgfs.update.dropped", reason="local-not-member",
                      spaceId=self.space_id, docId=frame.doc_id, peer=peer)
            return
        if frame.update is None:
            local = self.host.read_vectors((frame.doc_id,)).get(frame.doc_id) or EMPTY_VECTOR
            if not covers(local, frame.vector):
                self.schedule_sync_from(peer)
            return
        before = self._member_peers() if frame.doc_id == "meta" else None
        outcome = self.host.apply_remote(frame.doc_id, frame.update, frame.vector, peer)
        kind = outcome_kind(outcome)
        if kind == "pending":
            self.schedule_sync_from(peer)
        elif kind == "accepted" and before is not None:
            self._meta_accepted(peer, member, before)
        elif kind == "rejected":
            self._log("warn", "orgfs.update.rejected", docId=frame.doc_id,
                      code=getattr(outcome, "code", "rejected"), peer=peer)

    def _member_peers(self) -> frozenset[str]:
        """Peers known to hold this space whose device owner is a current member."""

        with self._worker_lock:
            known = (*self._known_peers, *self._observations)
        return frozenset(node for node in known if self._member_node(node))

    def member_peers(self) -> tuple[str, ...]:
        """Online peers known to hold this space that pass the node gate, sorted."""

        return tuple(sorted(node for node in self._member_peers() if self._supplier_allowed(node)))

    def _meta_accepted(self, sender: str, was_member: bool, before: frozenset[str]) -> None:
        """Pull from nodes an accepted meta update re-admitted (F7 ruling 2)."""

        if self._closed or not self._local_member():
            return
        after = self._member_peers()
        targets = [sender, *sorted(after)] if not was_member else sorted(after - before)
        for node in dict.fromkeys(targets):
            if self._supplier_allowed(node):
                self.schedule_sync_from(node)

    def _candidate_peers(self) -> list[str]:
        discovered: list[str] = []
        source = self._holder_discovery
        if source is not None:
            try:
                discovered = sorted(source() if callable(source) else source)
            except Exception:  # noqa: BLE001 - discovery is a hint
                discovered = []
        with self._worker_lock:
            seen = list(reversed(self._known_peers))
        return [
            peer for peer in dict.fromkeys([*discovered, *seen])
            if self._supplier_allowed(peer) and self._member_node(peer)
        ]

    def request_content(self, doc_id: str) -> None:
        if self._closed or not self._local_member():
            return
        with self._worker_lock:
            if self._closed:
                return
            if doc_id in self._priority:
                self._priority.remove(doc_id)
            self._priority.insert(0, doc_id)
            del self._priority[SYNC_CONTENT_DOCUMENT_LIMIT:]
        for peer in self._candidate_peers()[:1]:
            self.schedule_sync_from(peer)

    def maintain(self, now: float) -> None:
        if self._closed:
            return
        self._expire_temporary_blobs(now)
        with self._worker_lock:
            nodes = list(self._observations)
            self._dependency_retries.clear()
            wanted = bool(self._priority)
        wanted = wanted and self._local_member()
        for node in nodes:
            if not self._supplier_allowed(node):
                with self._worker_lock:
                    self._observations.pop(node, None)
        if wanted:
            for peer in self._candidate_peers()[:1]:
                self.schedule_sync_from(peer)

    def observations(self) -> tuple[PeerObservation, ...]:
        with self._worker_lock:
            current = sorted(self._observations.values(), key=lambda item: item.node)
        return tuple(
            item for item in current
            if self._supplier_allowed(item.node) and self._member_node(item.node)
        )

    def close(self, timeout: float = 5.0) -> None:
        if self._close_done:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        with self._worker_lock:
            self._closed = True
            registrations, self._registrations = self._registrations, []
        if self._stop_rebuild is not None:
            try:
                self._stop_rebuild()
            except Exception:  # noqa: BLE001 - the session may already be closed
                pass
            self._stop_rebuild = None
        for registration in reversed(registrations):
            registration.close()
        with self._ingress_guard:
            self._ingress_closing = True
            drained = self._ingress_guard.wait_for(
                lambda: self._ingress_pending == 0 and self._handlers_active == 0,
                timeout=max(0.0, deadline - time.monotonic()),
            )
        if not drained:
            raise TimeoutError("orgfs sync ingress did not drain")
        self._ingress_executor.shutdown(wait=False)
        self._worker_executor.shutdown(wait=False, cancel_futures=False)
        with self._worker_lock:
            pending = tuple(self._worker_futures)
        if pending:
            _done, unfinished = wait(pending, timeout=max(0.0, deadline - time.monotonic()))
            if unfinished:
                raise TimeoutError("orgfs sync workers did not drain")
        with self._worker_lock:
            self._sync_pending.clear()
            self._sync_rerun.clear()
            self._pull_backs.clear()
            self._priority.clear()
            waiters = [waiter for batch in self._blob_waiters.values() for waiter in batch]
            self._blob_waiters.clear()
        for waiter in waiters:
            waiter.set()
        self._close_done = True


__all__ = ["StateSpaceSync"]
