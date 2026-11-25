# Decision record (C3, 2026-11-25, design §4.2, §4.3, §4.6):
# - One round = one query: meta, tree and at most 128 content docs (read-first
#   requests ahead of SyncHost.pending_content, tree order).  The request is
#   built from SyncHost.read_vectors; nothing here holds a Doc.
# - The reply is decoded whole (frames.decode_reply) before the first
#   apply_remote, then applied meta -> tree -> content.  A blob entry is
#   fetched and sha256-checked before it is handed over; a failed fetch skips
#   that doc for this round only.
# - Rerun policy, bounded so no outcome loops forever:
#   * an accepted update plus leftovers (omitted docs, a failed blob, docs
#     still pending after the tree moved) reruns at once (§4.2 "立即排下一轮");
#   * Pending("dependency") reruns against the same peer even without progress
#     (§3.6), at most SYNC_DEPENDENCY_RETRY_LIMIT times in a row per peer;
#   * Rejected never reruns by itself (§3.7: the host deduplicates alarms;
#     sync only guarantees no loop).
# - The observation stores the supplier's meta/tree vectors with the local
#   commit generation read before the request was sent, so a commit made
#   during the round counts as "after" the observation (§4.6).  The generation
#   is SyncHost.local_generation() (§4.7 revision).
# - sync_from keeps the journal mesh's _sync_active / rerun bookkeeping
#   (network/sync.py MeshSync.sync_from) so SupplierSyncMixin's coalescing,
#   queue bound and hold behave exactly as before.
# - Design §9.2 (F1, 2026-11-25): a requester whose member row is still there
#   with ``removedAt`` (SyncHost.requester_removed) is answered with the meta
#   document only: the request's vectors are cut to ``meta`` before the page
#   is built, so no tree, content or temporary blob is read for it.  A
#   requester that never was a member is refused as before.  No node gate runs
#   on the supplier side of a pull; the requester's device is not consulted.
# - F4 (real-node finding R2-1): a reply's blob entry is fetched with
#   SYNC_BLOB_FETCH_TIMEOUT_SECONDS for every chunk, not with the round's query
#   timeout: a scheduled round's query timeout is the remainder of a few-second
#   budget, which a multi-chunk tree blob from a busy supplier cannot meet.
# - Package F6 (real-node finding R3-1): while SyncHost.local_member() is
#   false a round asks for no content document and applies only the meta
#   entry of the reply (membership is read again before each entry, so a meta
#   entry that re-admits this node lets the rest of the same reply through);
#   it reruns only when that meta made this node a member again, and never
#   for leftovers it was not allowed to take.
# - Package F7 (real-node finding R4-3, ruling 1, option (a)): a broadcast is
#   sent once, at commit, so updates whose broadcast was lost (or refused
#   then, acceptable now) live on one node until somebody pulls it.  The
#   request of a pull carries the requester's meta and tree vectors; when the
#   supplier is a current member, the requester's node passes presence and the
#   node gate, and the local vectors do not cover the requester's, the
#   supplier records them as the requester's observation (so its ``synced``
#   is false while the requester is ahead, ruling 3) and schedules a pull
#   from the requester through the usual coalescing and supplier backoff.
#   "No progress, stop": the vectors that triggered a pull back are kept per
#   requester, and the same vectors never trigger again; the entry is dropped
#   once the local vectors cover a request.  Without new writes a requester
#   can therefore trigger at most one pull back per distinct vector pair, so
#   two nodes cannot keep triggering each other.  A removed or never-invited
#   requester is answered as before and never pulled from.
# - Package F7 (ruling 2): an accepted meta update, broadcast or pulled, that
#   makes this node a member again schedules pulls from its sender and from
#   every peer known to hold the space (synced with, broadcast from or
#   observed) that passes the node gate, so each of them sees the vectors
#   this node wrote while it was refused and pulls them back.  One that
#   makes another member's known node pass the node gate again schedules a
#   pull from that node, so the re-admission converges whichever side
#   applies the new member table first.  Those updates are admitted under
#   the member table current when they arrive (result-state admission): a
#   write made while this node was already removed but did not know it is
#   accepted after a re-invite.
"""Requester rounds and the supplier's sync query handler."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import time
from typing import TYPE_CHECKING
import uuid

from hyprial.daemon.impl.orgfs.sync.frames import (
    ReplyEntry,
    SyncFailure,
    SyncReply,
    SyncRequest,
    decode_reply,
    decode_request,
    encode_error,
    encode_reply,
    encode_request,
    entry_value,
    json_bytes,
    ordered_ids,
)
from hyprial.daemon.impl.orgfs.sync.interface import (
    CATALOG_DOC_IDS,
    EMPTY_VECTOR,
    ORGFS_PAGE_BYTES,
    SYNC_BLOB_FETCH_TIMEOUT_SECONDS,
    SYNC_CONTENT_DOCUMENT_LIMIT,
    SYNC_DEPENDENCY_RETRY_LIMIT,
    PeerObservation,
    SyncError,
    outcome_kind,
)
from hyprial.daemon.impl.orgfs.sync.vectors import FrameError, covers

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.sync.interface import SyncHost
    from hyprial.daemon.impl.transport import KeySpace, TransportSession


def _b64_length(size: int) -> int:
    return (size + 2) // 3 * 4


class SyncPullMixin:
    """Round responsibilities of the sole StateSpaceSync state host."""

    session: TransportSession
    _keys: KeySpace
    host: SyncHost
    space_id: str
    node_id: str
    author: str

    def sync_from(
        self,
        supplier: str,
        *,
        timeout: float = 3.0,
        deadline_monotonic: float | None = None,
    ) -> int:
        """Run one round against ``supplier``; return the accepted count."""

        started = time.monotonic()
        with self._worker_lock:
            self._sync_active.add(supplier)
        try:
            accepted, rerun, metrics = self._round(supplier, timeout, deadline_monotonic)
            if rerun:
                with self._worker_lock:
                    self._sync_rerun.add(supplier)
            self._log(
                "info" if accepted else "debug",
                "orgfs.sync.completed",
                spaceId=self.space_id,
                supplier=supplier,
                importedCount=accepted,
                rerun=rerun,
                elapsedMs=int((time.monotonic() - started) * 1000),
                **metrics,
            )
            return accepted
        finally:
            with self._worker_lock:
                self._sync_active.discard(supplier)
                schedule_rerun = (
                    supplier in self._sync_rerun
                    and supplier not in self._sync_pending
                    and not self._closed
                )
                if schedule_rerun:
                    self._sync_rerun.discard(supplier)
            if schedule_rerun:
                self.schedule_sync_from(supplier)

    def _content_targets(self) -> list[str]:
        with self._worker_lock:
            chosen = self._priority[:SYNC_CONTENT_DOCUMENT_LIMIT]
            del self._priority[: len(chosen)]
        if len(chosen) < SYNC_CONTENT_DOCUMENT_LIMIT:
            for doc_id in self.host.pending_content(SYNC_CONTENT_DOCUMENT_LIMIT):
                if len(chosen) >= SYNC_CONTENT_DOCUMENT_LIMIT:
                    break
                if doc_id not in chosen and doc_id not in CATALOG_DOC_IDS:
                    chosen.append(doc_id)
        return chosen

    def _round(
        self, supplier: str, timeout: float, deadline: float | None
    ) -> tuple[int, bool, dict[str, int]]:
        if self._closed:
            return 0, False, {}
        if not self.host.supplier_allowed(supplier):
            raise SyncError(
                "not-a-member",
                "supplier's device owner is not a current member",
                reason="supplier-not-member",
            )
        generation = self.host.local_generation()
        member = self._local_member()
        content = self._content_targets() if member else []
        priority = [doc_id for doc_id in content]
        ids = ordered_ids([*CATALOG_DOC_IDS, *content])
        local = self.host.read_vectors(ids)
        request = SyncRequest(
            self.space_id,
            uuid.uuid4().hex,
            self.node_id,
            self.author,
            tuple((doc_id, local.get(doc_id) or EMPTY_VECTOR) for doc_id in ids),
            ORGFS_PAGE_BYTES,
        )
        payload = encode_request(request)
        query_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SyncError(
                    "no-holder-online", "sync deadline expired before the query",
                    reason="deadline-expired-mid-sync",
                )
            query_timeout = min(timeout, remaining)
        replies = self.session.query(
            self._keys.orgfs3_sync(self.space_id, supplier), payload, timeout=query_timeout
        )
        metrics = {
            "pages": 1,
            "requestBytes": len(payload),
            "responseBytes": sum(len(reply.payload) for reply in replies),
        }
        if not replies:
            raise SyncError("no-holder-online", "supplier did not answer", **metrics)
        try:
            reply = decode_reply(
                replies[0].payload,
                space_id=self.space_id,
                round=request.round,
                supplier=supplier,
                requested=frozenset(ids),
            )
        except FrameError as exc:
            self._log("warn", "orgfs.sync.reply-rejected", supplier=supplier, reason=str(exc))
            raise SyncError("invalid-argument", str(exc), reason="invalid-reply", **metrics) from exc
        if isinstance(reply, SyncFailure):
            raise SyncError(reply.code, reply.message, reason=reply.code, **metrics)
        accepted, dependency, incomplete = self._apply_reply(reply, supplier)
        with self._worker_lock:
            self._observations[supplier] = PeerObservation(
                supplier, reply.meta_vector, reply.tree_vector, generation
            )
            self._remember_peer(supplier)
            leftover_priority = [doc_id for doc_id in priority if doc_id in reply.omitted]
            self._priority[:0] = [d for d in leftover_priority if d not in self._priority]
            retries = self._dependency_retries.get(supplier, 0)
            retries = retries + 1 if dependency else 0
            self._dependency_retries[supplier] = retries
        if self._closed:
            return accepted, False, metrics
        member_now = self._local_member()
        leftovers = member_now and (
            bool(reply.omitted) or incomplete or bool(self.host.pending_content(1))
        )
        rerun = (accepted > 0 and leftovers) or (member_now and not member) or (
            dependency and retries <= SYNC_DEPENDENCY_RETRY_LIMIT
        )
        if dependency and retries > SYNC_DEPENDENCY_RETRY_LIMIT:
            self._log("warn", "orgfs.sync.dependency-unresolved", supplier=supplier,
                      spaceId=self.space_id, attempts=retries)
        return accepted, rerun, metrics

    def _apply_reply(self, reply: SyncReply, supplier: str) -> tuple[int, bool, bool]:
        order = {doc_id: index for index, doc_id in enumerate(ordered_ids(e.doc_id for e in reply.entries))}
        accepted = 0
        dependency = False
        incomplete = False
        for entry in sorted(reply.entries, key=lambda item: order[item.doc_id]):
            if self._closed:
                break
            if entry.doc_id != "meta" and not self._local_member():
                continue
            update = entry.update
            if update is None:
                try:
                    update = self._fetch_blob(
                        str(entry.blob), int(entry.size or 0), timeout=SYNC_BLOB_FETCH_TIMEOUT_SECONDS
                    )
                except SyncError as exc:
                    incomplete = True
                    self._log("warn", "orgfs.sync.blob-fetch-failed", supplier=supplier,
                              docId=entry.doc_id, digest=entry.blob, reason=exc.code)
                    continue
            if self._closed:
                break
            meta = entry.doc_id == "meta"
            was_member = self._local_member() if meta else True
            before = self._member_peers() if meta else frozenset()
            outcome = self.host.apply_remote(entry.doc_id, update, entry.vector, supplier)
            kind = outcome_kind(outcome)
            if kind == "accepted" and meta:
                self._meta_accepted(supplier, was_member, before)
            if kind == "accepted":
                accepted += 1
            elif kind == "pending":
                dependency = dependency or getattr(outcome, "reason", None) == "dependency"
            else:
                self._log("warn", "orgfs.update.rejected", docId=entry.doc_id,
                          code=getattr(outcome, "code", "rejected"), peer=supplier)
        return accepted, dependency, incomplete

    def _handle_sync(self, selector: str, payload: bytes | None) -> Iterable[tuple[str, bytes]]:
        if not self._enter_handler():
            return ()
        try:
            return ((selector, self._answer_sync(payload)),)
        finally:
            self._leave_handler()

    def _answer_sync(self, payload: bytes | None) -> bytes:
        try:
            request = decode_request(payload, space_id=self.space_id)
        except FrameError as exc:
            return encode_error(space_id=self.space_id, round="", supplier=self.node_id,
                                code="invalid-argument", message=str(exc))

        def refuse(code: str, message: str) -> bytes:
            return encode_error(space_id=self.space_id, round=request.round,
                                supplier=self.node_id, code=code, message=message)

        try:
            if not self.host.requester_allowed(request.requester_author):
                if not self.host.requester_removed(request.requester_author):
                    return refuse("not-a-member", "requester is not a current member")
                request = replace(request, vectors=tuple(
                    (doc_id, since) for doc_id, since in request.vectors if doc_id == "meta"
                ))
            else:
                self._pull_back_if_ahead(request)
            return encode_reply(self._build_page(request))
        except Exception as exc:  # noqa: BLE001 - a query handler must answer
            self._log("warn", "orgfs.sync.serve-failed", requester=request.requester_node,
                      reason=getattr(exc, "code", type(exc).__name__))
            return refuse(str(getattr(exc, "code", "unavailable")), str(exc))

    def _pull_back_if_ahead(self, request: SyncRequest) -> None:
        """Pull from a member requester whose catalog vectors this node does not cover."""

        node = request.requester_node
        sent = dict(request.vectors)
        if (
            node == self.node_id
            or any(doc_id not in sent for doc_id in CATALOG_DOC_IDS)
            or not self._local_member()
            or not self._supplier_allowed(node)
            or not self._member_node(node)
        ):
            return
        theirs = (sent["meta"], sent["tree"])
        generation = self.host.local_generation()
        local = self.host.read_vectors(CATALOG_DOC_IDS)
        if all(covers(local.get(doc_id) or EMPTY_VECTOR, vector)
               for doc_id, vector in zip(CATALOG_DOC_IDS, theirs)):
            with self._worker_lock:
                self._pull_backs.pop(node, None)
            return
        with self._worker_lock:
            if self._closed or self._pull_backs.get(node) == theirs:
                return
            self._pull_backs[node] = theirs
            seen = self._observations.get(node)
            if seen is None or not (covers(seen.meta_vector, theirs[0])
                                    and covers(seen.tree_vector, theirs[1])):
                self._observations[node] = PeerObservation(node, theirs[0], theirs[1], generation)
            self._remember_peer(node)
        self._log("info", "orgfs.sync.requester-ahead", spaceId=self.space_id, requester=node)
        self.schedule_sync_from(node)

    def _build_page(self, request: SyncRequest) -> SyncReply:
        budget = min(request.max_bytes, ORGFS_PAGE_BYTES)
        catalog = self.host.read_vectors(CATALOG_DOC_IDS)
        meta_vector = catalog.get("meta") or EMPTY_VECTOR
        tree_vector = catalog.get("tree") or EMPTY_VECTOR
        empty = SyncReply(self.space_id, request.round, self.node_id, (), meta_vector, tree_vector, ())
        used = len(encode_reply(empty)) + sum(len(json_bytes({"d": d})) for d, _ in request.vectors)
        entries: list[ReplyEntry] = []
        omitted: list[str] = []
        full = False
        for doc_id, since in request.vectors:
            if full:
                omitted.append(doc_id)
                continue
            found = self.host.read_update(doc_id, since)
            if found is None:
                continue
            update, vector = found
            if covers(since, vector):
                continue
            if _b64_length(len(update)) > budget // 2:
                digest = self._put_temporary_blob(update)
                if digest is None:
                    omitted.append(doc_id)
                    continue
                entry = ReplyEntry(doc_id, vector, blob=digest, size=len(update))
            else:
                entry = ReplyEntry(doc_id, vector, update=update)
            cost = len(json_bytes(entry_value(entry))) + 1
            if entries and used + cost > budget:
                full = True
                omitted.append(doc_id)
                continue
            entries.append(entry)
            used += cost
        reply = SyncReply(self.space_id, request.round, self.node_id, tuple(entries),
                          meta_vector, tree_vector, tuple(omitted))
        while entries and len(encode_reply(reply)) > ORGFS_PAGE_BYTES:
            omitted.insert(0, entries.pop().doc_id)
            reply = SyncReply(self.space_id, request.round, self.node_id, tuple(entries),
                              meta_vector, tree_vector, tuple(omitted))
        return reply


__all__ = ["SyncPullMixin"]
