# Decision record (C3, 2026-11-25, design §4.3 blob fallback):
# - The blob plane is reused unchanged: key orgfs_blob(digest), the chunk
#   codecs and BlobStore from storage/blobs.py.  The journal mesh's
#   fetch_blob (network/sync.py) and _handle_blob (network/queries.py) are
#   methods bound to OrgFsMesh state (store, replica, member gate), so they are
#   re-expressed here against the same codecs instead of imported; the
#   chunk loop, the "absence answers never decide" rule and the final sha256
#   check through blobs.assemble are kept as they were.
# - The handler key is KeySpace.orgfs_blob_any() (§4.7 revision).
# - request_blob / fetch_blob (§4.7 revision) follow OrgFsMesh.request_blob
#   (network/recovery.py) and MeshSync.fetch_blob (network/sync.py): one
#   coalesced worker job per digest whose waiters share one Event, at most 32
#   waiting digests (overflow sets the Event at once and logs
#   orgfs.content.blob-fetch-dropped), candidates from holder_discovery and
#   peers seen, each gated by presence and SyncHost.supplier_allowed, fetched
#   bytes pinned under "replica".  The Event means "this attempt ended", not
#   "the blob arrived".  fetch_blob raises SyncError where the mesh raised
#   StoreError; the phase D caller (composition/content.py) must catch it.
# - A temporary sync blob is pinned under reason "sync" and remembered with
#   its expiry.  Expiry calls BlobStore.unpin(space, digest, "sync") (phase D
#   P0; C3 report §4-8), which drops only that reason, so a digest the space
#   also holds for another reason keeps those rows; the bytes are deleted only
#   when no space references the digest any more.
# - The handler answers only for its own space; other spaces' answers are
#   "unknown-space", an absence code, matching the old replica path.
# - Package F6 (real-node finding R3-1): every blob request names its
#   requester (node and author, the same self-reported source as the sync
#   request) and the handler applies the pull rule to the author: a requester
#   that is not a current member, removed or never invited, gets
#   ``not-a-member`` and no chunk.  That code is not an absence answer, so it
#   decides the fetch.  A node that is not a current member itself requests
#   no blob: request_blob ends at once and fetch_blob raises ``not-a-member``
#   with reason ``local-not-member``.
"""Temporary sync blobs: serve chunks, fetch and verify, expire."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import Future
import json
from threading import Event
import time
from typing import TYPE_CHECKING, Any

from hyprial.daemon.impl.orgfs.storage.blobs import (
    ORGFS_BLOB_CHUNK_BYTES,
    BlobError,
    assemble,
    decode_chunk,
    encode_error,
    encode_request,
)
from hyprial.daemon.impl.orgfs.storage.blobs import decode_request as decode_blob_request
from hyprial.daemon.impl.orgfs.sync.interface import (
    SYNC_BLOB_FETCH_QUEUE_LIMIT,
    SYNC_BLOB_FETCH_TIMEOUT_SECONDS,
    SYNC_BLOB_REASON,
    SYNC_BLOB_TTL_SECONDS,
    SYNC_FETCHED_BLOB_REASON,
    SyncError,
)

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.storage.blobs import BlobStore
    from hyprial.daemon.impl.transport import KeySpace, TransportSession

_ABSENCE_CODES = frozenset({"unknown-blob", "unknown-space"})


def blob_wildcard(keys: KeySpace) -> str:
    return keys.orgfs_blob_any()


def _parse(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class SyncBlobMixin:
    """Blob responsibilities of the sole StateSpaceSync state host."""

    session: TransportSession
    _keys: KeySpace
    space_id: str
    node_id: str
    blob_store: BlobStore | None

    def _blob_error(self, code: str, message: str, details: dict[str, Any] | None = None) -> bytes:
        return encode_error(
            code, message, {**(details or {}), "nodeId": self.node_id, "spaceId": self.space_id}
        )

    def _handle_blob(self, selector: str, payload: bytes | None) -> Iterable[tuple[str, bytes]]:
        if not self._enter_handler():
            return ()
        try:
            if self.blob_store is None:
                return ((selector, self._blob_error("unknown-blob", "blob store is unavailable")),)
            try:
                value = decode_blob_request(payload or b"")
                if value["spaceId"] != self.space_id:
                    return ((selector, self._blob_error("unknown-space", "blob requested another space")),)
                author = value["requester"]["author"]
                if not self.host.requester_allowed(author):
                    reason = "requester-removed" if self.host.requester_removed(author) else "requester-not-member"
                    return ((selector, self._blob_error("not-a-member", "requester is not a current member",
                                                        {"reason": reason})),)
                return ((selector, self.blob_store.get_chunk(value)),)
            except BlobError as exc:
                return ((selector, self._blob_error(exc.code, str(exc), exc.details)),)
            except (TypeError, ValueError) as exc:
                return ((selector, self._blob_error("invalid-argument", str(exc))),)
        finally:
            self._leave_handler()

    def _put_temporary_blob(self, update: bytes) -> str | None:
        store = self.blob_store
        if store is None:
            return None
        digest = store.put(self.space_id, update, reason=SYNC_BLOB_REASON)
        with self._worker_lock:
            self._temporary_blobs[digest] = time.monotonic() + SYNC_BLOB_TTL_SECONDS
        return digest

    def _expire_temporary_blobs(self, now: float) -> None:
        store = self.blob_store
        if store is None:
            return
        with self._worker_lock:
            expired = [digest for digest, until in self._temporary_blobs.items() if until <= now]
            for digest in expired:
                del self._temporary_blobs[digest]
        for digest in expired:
            try:
                store.unpin(self.space_id, digest, SYNC_BLOB_REASON)
                store.delete_if_unreferenced(digest)
            except Exception as exc:  # noqa: BLE001 - expiry is retried by nothing; log only
                self._log("warn", "orgfs.sync.blob-expire-failed", digest=digest, detail=str(exc))

    def fetch_blob(self, holder: str, digest: str, *, size_hint: int | None = None) -> bytes:
        """Fetch one blob through the blob channel and verify its sha256."""

        if not self._local_member():
            raise SyncError("not-a-member", "this node is not a current member of the space",
                            reason="local-not-member")
        if not self._supplier_allowed(holder):
            raise SyncError("not-a-member", "blob supplier is not an online host")
        if not self.host.supplier_allowed(holder):
            raise SyncError("not-a-member", "supplier's device owner is not a current member",
                            reason="supplier-not-member")
        return self._fetch_blob(digest, size_hint, timeout=SYNC_BLOB_FETCH_TIMEOUT_SECONDS, exact=False)

    def request_blob(self, digest: str) -> Event:
        """Join one bounded, coalesced fetch; the Event is not proof of arrival."""

        completed = Event()
        store = self.blob_store
        if store is None or store.contains(digest) or self._closed or not self._local_member():
            completed.set()
            return completed
        rejected: str | None = None
        future: Future[object] | None = None
        with self._worker_lock:
            if self._closed:
                completed.set()
                return completed
            existing = self._blob_waiters.get(digest)
            if existing:
                return existing[0]
            if len(self._blob_waiters) >= SYNC_BLOB_FETCH_QUEUE_LIMIT:
                rejected = "queue-full"
            else:
                self._blob_waiters[digest] = [completed]
                try:
                    future = self._worker_executor.submit(self._run_blob_request, digest)
                except RuntimeError:
                    self._blob_waiters.pop(digest, None)
                    rejected = "worker-closed"
                else:
                    self._worker_futures.add(future)
        if future is not None:
            future.add_done_callback(self._worker_finished)
        if rejected is not None:
            completed.set()
            self._log("warn", "orgfs.content.blob-fetch-dropped", reason=rejected,
                      digest=digest, queueLimit=SYNC_BLOB_FETCH_QUEUE_LIMIT)
        return completed

    def _run_blob_request(self, digest: str) -> None:
        try:
            for holder in self._candidate_peers():
                if self._closed or not self._local_member():
                    break
                try:
                    if not self.host.supplier_allowed(holder):
                        continue
                    payload = self._fetch_blob(digest, None, timeout=SYNC_BLOB_FETCH_TIMEOUT_SECONDS, exact=False)
                    store = self.blob_store
                    if store is not None and store.put(self.space_id, payload, reason=SYNC_FETCHED_BLOB_REASON) == digest:
                        break
                except Exception as exc:  # noqa: BLE001 - try the next holder
                    self._log("warn", "orgfs.update.blob-fetch-failed", supplier=holder, digest=digest,
                              reason=getattr(exc, "code", type(exc).__name__))
        finally:
            with self._worker_lock:
                waiters = self._blob_waiters.pop(digest, [])
            for waiter in waiters:
                waiter.set()

    def _fetch_blob(self, digest: str, size: int | None, *, timeout: float, exact: bool = True) -> bytes:
        chunks: list[bytes] = []
        offset = 0
        total = size if exact else None
        started = time.monotonic()
        while total is None or offset < total:
            hint = size if total is None else total
            length = min(ORGFS_BLOB_CHUNK_BYTES, hint - offset) if type(hint) is int and hint > offset else None
            replies = self.session.query(
                self._keys.orgfs_blob(digest),
                encode_request(self.space_id, digest, offset, length,
                               requester_node=self.node_id, requester_author=self.author),
                timeout=timeout,
                all_replies=True,
            )
            chosen: bytes | None = None
            decisive: list[dict[str, Any]] = []
            for reply in replies:
                value = _parse(reply.payload)
                if value.get("type") == "orgfs-blob-chunk":
                    chosen = reply.payload
                    break
                if value.get("type") == "orgfs-error" and str(value.get("code")) not in _ABSENCE_CODES:
                    decisive.append(value)
            if chosen is None:
                code = str(decisive[0].get("code")) if decisive else "blob-unavailable"
                raise SyncError(code, "no holder returned a blob chunk", digest=digest,
                                elapsedMs=int((time.monotonic() - started) * 1000))
            try:
                chunk = decode_chunk(chosen)
            except (BlobError, ValueError) as exc:
                raise SyncError("blob-unavailable", str(exc), digest=digest) from exc
            if (exact and chunk["total"] != size) or chunk["offset"] != offset:
                raise SyncError("blob-unavailable", "blob chunk does not match the reply size", digest=digest)
            total = int(chunk["total"])
            chunks.append(chosen)
            offset += int(chunk["length"])
            if total == 0:
                break
        try:
            return assemble(digest, chunks)
        except BlobError as exc:
            raise SyncError("blob-unavailable", str(exc), digest=digest) from exc


__all__ = ["SyncBlobMixin", "blob_wildcard"]
