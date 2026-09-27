"""Orgfs replication over the payload-bearing transport seam.

The journal remains authoritative: this module only publishes and replays the
exact envelope bytes owned by :mod:`hyprial.orgfs.store`.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Lock
import time
from typing import TYPE_CHECKING, Any, Final

from hyprial.transport import KeySpace, Registration, TransportSample, TransportSession

from .api import OrgFsError
from .blobs import (
    ORGFS_BLOB_CHUNK_BYTES,
    BlobError,
    BlobStore,
    assemble,
    decode_chunk,
    decode_request as decode_blob_request,
    encode_chunk,
    encode_request,
)
from .store import (
    ORGFS_ENVELOPE_BYTES,
    ORGFS_PAGE_BYTES,
    ImportResult,
    LocalSpaceStore,
    StoreError,
)

if TYPE_CHECKING:
    from .replica import ReplicaStore


ORGFS_LOG_RANGE_LIMIT: Final[int] = 256
ORGFS_WIRE_VERSION: Final[int] = 1
ORGFS_BLOB_FETCH_QUEUE_LIMIT: Final[int] = 32
ORGFS_ANNOUNCE_BUFFER_LIMIT: Final[int] = 256
ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT: Final[int] = 256
ORGFS_SYNC_QUEUE_LIMIT: Final[int] = 32
ORGFS_REPLICA_SYNC_SCAN_ROWS: Final[int] = 256
MeshLogger = Callable[..., None]
SupplierGate = Callable[[str], bool]
AppliedHook = Callable[[bytes], None]
ReplacementHook = Callable[[str, str, bytes], None]
AnnouncementSource = Callable[[], Iterable[Mapping[str, Any]]]


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _error(code: str, message: str, **details: object) -> bytes:
    return _json_bytes(
        {
            "schemaVersion": 1,
            "type": "orgfs-error",
            "code": code,
            "message": message,
            "details": details,
        }
    )


def _decode_request(payload: bytes | None) -> dict[str, Any]:
    if payload is None:
        raise StoreError("invalid-argument", "orgfs query requires a JSON payload")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(
            "invalid-argument", "orgfs query payload is invalid JSON"
        ) from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise StoreError("invalid-argument", "unsupported orgfs query schema")
    return value


def _decode_update_header(envelope: bytes, *, space_id: str) -> dict[str, Any]:
    """Validate the transport-visible update schema before supplier admission."""

    try:
        value = json.loads(envelope.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError("invalid-argument", "orgfs update is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != ORGFS_WIRE_VERSION
        or value.get("type") != "orgfs-update"
        or value.get("spaceId") != space_id
    ):
        raise StoreError("invalid-argument", "invalid orgfs-update envelope")
    if not isinstance(value.get("docId"), str) or not value["docId"]:
        raise StoreError("invalid-argument", "orgfs update requires docId")
    seq = value.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise StoreError("invalid-argument", "orgfs update has an invalid sequence")
    origin = value.get("origin")
    required_origin = (
        "writer",
        "node",
        "author",
        "actor",
        "committedAt",
        "metaFrontier",
    )
    if not isinstance(origin, dict) or any(
        field not in origin for field in required_origin
    ):
        raise StoreError("invalid-argument", "orgfs update has an incomplete origin")
    if not isinstance(origin.get("node"), str) or not origin["node"]:
        raise StoreError("invalid-argument", "orgfs update origin requires a node")
    if ("update" in value) == ("updateBlob" in value):
        raise StoreError(
            "invalid-argument", "orgfs update requires exactly one update body"
        )
    return value


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise StoreError("invalid-argument", "version vector must be base64 text")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise StoreError(
            "invalid-argument", "version vector is not valid base64"
        ) from exc


class OrgFsMesh:
    """Broadcast, query, import, and probe one local orgfs space."""

    def __init__(
        self,
        session: TransportSession,
        store: LocalSpaceStore,
        node_id: str,
        *,
        author: str,
        blob_store: BlobStore | None = None,
        supplier_online: SupplierGate | None = None,
        on_applied: AppliedHook | None = None,
        on_replacement: ReplacementHook | None = None,
        announcement_source: AnnouncementSource | None = None,
        holder_discovery: set[str] | None = None,
        durable: bool = True,
        logger: MeshLogger | None = None,
        keys: KeySpace | None = None,
        replica_store: ReplicaStore | None = None,
    ) -> None:
        if not node_id:
            raise ValueError("node_id must not be empty")
        self.session = session
        self.store = store
        self.node_id = node_id
        self.author = author
        self.blob_store = blob_store
        self.durable = bool(durable)
        self._supplier_online = supplier_online or (lambda _supplier: True)
        self._on_applied = on_applied
        self._on_replacement = on_replacement
        self._announcement_source = announcement_source
        self._holder_discovery = (
            holder_discovery if holder_discovery is not None else set()
        )
        self._logger = logger
        self._keys = keys or KeySpace()
        self.replica_store = replica_store
        self._closed = False
        self._worker_lock = Lock()
        self._blob_fetch_pending: dict[str, list[tuple[bytes, str]]] = {}
        self._blob_fetch_pending_count = 0
        self._blob_fetch_dropped = 0
        self._sync_pending: set[str] = set()
        self._sync_active: set[str] = set()
        self._sync_dropped = 0
        self._worker_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"orgfs-worker-{store.space_id[:12]}",
        )
        self._registrations: list[Registration] = []
        self._registrations.append(
            session.subscribe(self._keys.orgfs_log_any(store.space_id), self._on_log)
        )
        try:
            self._registrations.append(
                session.declare_query_handler(
                    self._keys.orgfs_sync(store.space_id, node_id), self._handle_sync
                )
            )
            self._registrations.append(
                session.declare_query_handler(
                    self._keys.orgfs_log_any(store.space_id), self._handle_log_range
                )
            )
            if blob_store is not None:
                self._registrations.append(
                    session.declare_query_handler(
                        f"{self._keys.prefix}/org/fs/blob/*", self._handle_blob
                    )
                )
        except BaseException:
            self.close()
            raise

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger(level, event, **fields)

    def _supplier_allowed(self, supplier: str) -> bool:
        try:
            return bool(supplier) and bool(self._supplier_online(supplier))
        except Exception:
            return False

    @property
    def _announced_nodes(self) -> set[str]:
        """Deprecated test view of runtime discovery; never an admission gate."""

        return self._holder_discovery

    def receive(self, envelope: bytes, *, supplier: str) -> ImportResult:
        """Apply all three inbound gates without turning rejection into a reply."""

        return self._receive(envelope, supplier=supplier, defer_blob_fetch=False)

    def _receive(
        self,
        envelope: bytes,
        *,
        supplier: str,
        defer_blob_fetch: bool,
    ) -> ImportResult:
        if len(envelope) > ORGFS_ENVELOPE_BYTES:
            return ImportResult("rejected", "too-large")
        try:
            _decode_update_header(envelope, space_id=self.store.space_id)
        except StoreError as exc:
            self._log(
                "warn", "orgfs.update.rejected", reason=exc.code, supplier=supplier
            )
            return ImportResult("rejected", exc.code)
        if not self._supplier_allowed(supplier):
            self._log(
                "warn",
                "orgfs.update.rejected",
                reason="supplier-offline",
                supplier=supplier,
            )
            return ImportResult("rejected", "supplier-offline")
        result = self.store.import_envelope(envelope, supplier=supplier)
        details = getattr(result, "details", None)
        if (
            result.status == "rejected"
            and result.code == "unknown-blob"
            and isinstance(details, Mapping)
            and isinstance(details.get("digest"), str)
            and self.blob_store is not None
        ):
            digest = str(details["digest"])
            if defer_blob_fetch:
                self._defer_blob_recovery(envelope, supplier=supplier, digest=digest)
                return result
            result = self._recover_blob(
                envelope, supplier=supplier, digest=digest, initial=result
            )
        self._finish_receive(envelope, supplier=supplier, result=result)
        return result

    def _recover_blob(
        self,
        envelope: bytes,
        *,
        supplier: str,
        digest: str,
        initial: ImportResult,
    ) -> ImportResult:
        assert self.blob_store is not None
        try:
            self._fetch_blob_into_store(supplier, digest)
            # Exactly one retry: a repeated rejection is final.
            return self.store.import_envelope(envelope, supplier=supplier)
        except Exception as exc:  # noqa: BLE001 - inbound fetch must not kill the worker
            self._log(
                "warn",
                "orgfs.update.blob-fetch-failed",
                reason=getattr(exc, "code", "blob-unavailable"),
                supplier=supplier,
                digest=digest,
            )
            return initial

    def _fetch_blob_into_store(self, supplier: str, digest: str) -> None:
        assert self.blob_store is not None
        blob = self.fetch_blob(supplier, digest)
        stored_digest = self.blob_store.put(self.store.space_id, blob, reason="replica")
        if stored_digest != digest:
            raise StoreError(
                "blob-unavailable", "fetched blob did not match requested digest"
            )

    def _defer_blob_recovery(
        self, envelope: bytes, *, supplier: str, digest: str
    ) -> None:
        submit = False
        dropped_count: int | None = None
        queue_depth = 0
        with self._worker_lock:
            if self._closed:
                return
            if self._blob_fetch_pending_count >= ORGFS_BLOB_FETCH_QUEUE_LIMIT:
                self._blob_fetch_dropped += 1
                dropped_count = self._blob_fetch_dropped
                queue_depth = self._blob_fetch_pending_count
            else:
                batch = self._blob_fetch_pending.get(digest)
                if batch is None:
                    batch = []
                    self._blob_fetch_pending[digest] = batch
                    submit = True
                batch.append((envelope, supplier))
                self._blob_fetch_pending_count += 1
        if dropped_count is not None:
            self._log(
                "warn",
                "orgfs.update.blob-fetch-dropped",
                reason="queue-full",
                supplier=supplier,
                digest=digest,
                droppedCount=dropped_count,
                queueDepth=queue_depth,
                queueLimit=ORGFS_BLOB_FETCH_QUEUE_LIMIT,
            )
            return
        if not submit:
            return
        try:
            self._worker_executor.submit(self._run_blob_recovery, digest)
        except RuntimeError:
            with self._worker_lock:
                batch = self._blob_fetch_pending.pop(digest, [])
                self._blob_fetch_pending_count -= len(batch)

    def _run_blob_recovery(self, digest: str) -> None:
        fetched = False
        attempted: set[str] = set()
        while True:
            with self._worker_lock:
                batch = self._blob_fetch_pending.get(digest, ())
                supplier = next(
                    (
                        candidate
                        for _envelope, candidate in batch
                        if candidate not in attempted
                    ),
                    None,
                )
                if supplier is None:
                    pending = self._blob_fetch_pending.pop(digest, [])
                    self._blob_fetch_pending_count -= len(pending)
                    break
            attempted.add(supplier)
            try:
                self._fetch_blob_into_store(supplier, digest)
                fetched = True
            except Exception as exc:  # noqa: BLE001 - keep the serial worker alive
                self._log(
                    "warn",
                    "orgfs.update.blob-fetch-failed",
                    reason=getattr(exc, "code", "blob-unavailable"),
                    supplier=supplier,
                    digest=digest,
                )
                continue
            with self._worker_lock:
                pending = self._blob_fetch_pending.pop(digest, [])
                self._blob_fetch_pending_count -= len(pending)
            break

        for envelope, supplier in pending:
            initial = ImportResult(
                "rejected", "unknown-blob", details={"digest": digest}
            )
            result = (
                self.store.import_envelope(envelope, supplier=supplier)
                if fetched
                else initial
            )
            self._finish_receive(envelope, supplier=supplier, result=result)

    def schedule_sync_from(self, supplier: str) -> bool:
        """F6: coalesce one bounded anti-entropy job per live peer."""

        dropped_count: int | None = None
        with self._worker_lock:
            if (
                self._closed
                or supplier in self._sync_pending
                or supplier in self._sync_active
            ):
                return False
            if len(self._sync_pending) >= ORGFS_SYNC_QUEUE_LIMIT:
                self._sync_dropped += 1
                dropped_count = self._sync_dropped
            else:
                self._sync_pending.add(supplier)
        if dropped_count is not None:
            self._log(
                "warn",
                "orgfs.sync.dropped",
                reason="queue-full",
                supplier=supplier,
                droppedCount=dropped_count,
                queueDepth=ORGFS_SYNC_QUEUE_LIMIT,
                queueLimit=ORGFS_SYNC_QUEUE_LIMIT,
            )
            return False
        try:
            self._worker_executor.submit(self._run_scheduled_sync, supplier)
        except RuntimeError:
            with self._worker_lock:
                self._sync_pending.discard(supplier)
            return False
        return True

    def _run_scheduled_sync(self, supplier: str) -> None:
        try:
            self.sync_from(supplier)
        except Exception as exc:  # noqa: BLE001 - anti-entropy is best effort
            self._log(
                "warn",
                "orgfs.sync.failed",
                reason=getattr(exc, "code", "sync-failed"),
                supplier=supplier,
            )
        finally:
            with self._worker_lock:
                self._sync_pending.discard(supplier)

    def _finish_receive(
        self, envelope: bytes, *, supplier: str, result: ImportResult
    ) -> None:
        if result.status == "applied":
            self._store_replica_envelope(envelope)
            drained_for_replica = (result.details or {}).get("drained", ())
            if isinstance(drained_for_replica, (tuple, list)):
                for pending_envelope in drained_for_replica:
                    if isinstance(pending_envelope, bytes):
                        self._store_replica_envelope(pending_envelope)
        if (
            result.status == "applied"
            and result.code != "duplicate"
            and self._on_applied is not None
        ):
            self._on_applied(envelope)
            drained = (result.details or {}).get("drained", ())
            if isinstance(drained, (tuple, list)):
                for pending_envelope in drained:
                    if isinstance(pending_envelope, bytes):
                        self._on_applied(pending_envelope)
        if result.status == "applied" and result.code != "duplicate":
            try:
                value = json.loads(envelope)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = {}
            if value.get("docId") == "meta" and self.store.pending_replacements():
                self.schedule_sync_from(supplier)
        if result.status == "rejected":
            fields: dict[str, object] = {
                "reason": result.code or "rejected",
                "supplier": supplier,
            }
            try:
                decoded = json.loads(envelope)
                origin = decoded.get("origin", {})
                fields.update(
                    writer=origin.get("writer"),
                    seq=decoded.get("seq"),
                    metaFrontier=origin.get("metaFrontier"),
                )
            except Exception:
                pass
            self._log("warn", "orgfs.update.rejected", **fields)

    def _on_log(self, sample: TransportSample) -> None:
        try:
            value = _decode_update_header(sample.payload, space_id=self.store.space_id)
        except StoreError as exc:
            self._log("warn", "orgfs.update.rejected", reason=exc.code, supplier="")
            return
        # TransportSample exposes no Zenoh source identity.  The trusted-tailnet
        # fallback is origin.node, gated by the daemon's same online-host presence
        # judgment used by org.fetch.  Announcements discover holders only.
        supplier = str(value["origin"]["node"])
        if not self._supplier_allowed(supplier):
            self._log(
                "warn",
                "orgfs.update.rejected",
                reason="supplier-offline",
                supplier=supplier,
            )
            return
        self._receive(sample.payload, supplier=supplier, defer_blob_fetch=True)

    def broadcast_pending(self, space_id: str | None = None) -> int:
        if space_id is not None and space_id != self.store.space_id:
            return 0
        count = 0
        for record in self.store.unbroadcast():
            value = json.loads(record.envelope_bytes)
            self._store_replica_envelope(record.envelope_bytes)
            key = self._keys.orgfs_log(
                self.store.space_id,
                str(value["docId"]),
                record.writer,
                record.seq,
            )
            self.session.put(key, record.envelope_bytes)
            self.store.mark_broadcast(
                record.writer,
                record.seq,
                doc_id=str(value["docId"]),
            )
            count += 1
        return count

    def announce(
        self, *, mode: str = "rw", roles: tuple[str, ...] = ("member",)
    ) -> None:
        spaces = (
            list(self._announcement_source())
            if self._announcement_source is not None
            else [
                {
                    "spaceId": self.store.space_id,
                    "roles": list(roles),
                    "mode": mode,
                }
            ]
        )
        payload = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-announce",
                "node": self.node_id,
                "durable": self.durable,
                "spaces": spaces,
            }
        )
        if len(payload) > ORGFS_ENVELOPE_BYTES:
            payload = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-announce",
                    "node": self.node_id,
                    "durable": self.durable,
                    "spaces": [
                        {"spaceId": str(space["spaceId"])}
                        for space in spaces
                        if isinstance(space.get("spaceId"), str)
                    ],
                }
            )
        self.session.put(self._keys.orgfs_announce(self.node_id), payload)

    def attach_replica(self, replica_store: ReplicaStore) -> None:
        """Attach and prime the resident backend from the admitted journal."""

        if replica_store.space_id != self.store.space_id:
            raise ValueError("replica store belongs to another space")
        self.replica_store = replica_store
        self.durable = replica_store.durable()
        replica_store.prime_from(self.store, self.blob_store)
        if self.blob_store is not None:
            for digest in replica_store.pinned_blobs():
                self.blob_store.pin(self.store.space_id, digest, "replica")
        self.announce()

    def _store_replica_envelope(self, envelope: bytes) -> None:
        if self.replica_store is None:
            return
        try:
            value = _decode_update_header(envelope, space_id=self.store.space_id)
            origin = value["origin"]
            key = (
                f"log/{self.store.space_id}/{value['docId']}/"
                f"{origin['writer']}/{value['seq']}"
            )
            self.replica_store.store_envelope(key, envelope)
            digest = value.get("updateBlob")
            if isinstance(digest, str) and self.blob_store is not None:
                self.replica_store.store_blob(
                    digest, self.blob_store.get(self.store.space_id, digest)
                )
                self.blob_store.pin(self.store.space_id, digest, "replica")
        except Exception as exc:
            self._log(
                "error",
                "orgfs.replica.store-failed",
                reason=getattr(exc, "code", "replica-store-failed"),
                detail=str(exc),
            )
            # Replica conflicts are integrity alarms, not subscriber failures.
            # ReplicaStore has already preserved the original bytes and emitted
            # both callbacks.  The admitted member journal remains authoritative,
            # so the subscriber and local broadcast flush must continue.

    def _requester_allowed(self, value: Mapping[str, Any]) -> bool:
        requester = value.get("requester")
        return (
            isinstance(requester, dict)
            and isinstance(requester.get("author"), str)
            and self.store.member_mode(str(requester["author"])) is not None
        )

    @staticmethod
    def _cursor_fingerprint(value: Mapping[str, Any], doc_ids: tuple[str, ...]) -> str:
        body = {
            "spaceId": value.get("spaceId"),
            "docIds": doc_ids,
            "versionVectors": value.get("versionVectors", {}),
        }
        return hashlib.sha256(_json_bytes(body)).hexdigest()[:24]

    def _flatten_missing(
        self, doc_ids: tuple[str, ...], vectors: Mapping[str, Any]
    ) -> list[bytes]:
        missing: list[bytes] = []
        for doc_id in doc_ids:
            vv = _unb64(vectors.get(doc_id, ""))
            cursor: str | None = None
            while True:
                page = self.store.export_since(
                    doc_id, vv, cursor=cursor, max_bytes=ORGFS_PAGE_BYTES
                )
                missing.extend(page.envelopes)
                cursor = page.next
                if cursor is None:
                    break
        return missing

    def _sync_page(self, value: Mapping[str, Any]) -> bytes:
        if str(value.get("spaceId")) != self.store.space_id:
            raise StoreError("unknown-space", "sync requested another space")
        if not self._requester_allowed(value):
            raise StoreError("not-a-member", "requester is not a space member")
        raw_doc_ids = value.get("docIds")
        if raw_doc_ids is None:
            doc_ids = self.store.document_ids()
        elif isinstance(raw_doc_ids, list) and all(
            isinstance(item, str) for item in raw_doc_ids
        ):
            requested = tuple(dict.fromkeys(str(item) for item in raw_doc_ids))
            doc_ids = tuple(
                item
                for item in ("meta", *requested)
                if item in requested or item == "meta"
            )
        else:
            raise StoreError("invalid-argument", "docIds must be an array or null")
        # Meta is protocolically first even when callers omit it.
        doc_ids = tuple(dict.fromkeys(("meta", *doc_ids)))
        vectors = value.get("versionVectors", {})
        if not isinstance(vectors, dict):
            raise StoreError("invalid-argument", "versionVectors must be an object")
        max_bytes = value.get("maxBytes", ORGFS_PAGE_BYTES)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 0 < max_bytes <= ORGFS_PAGE_BYTES
        ):
            raise StoreError(
                "invalid-argument", "maxBytes is outside the allowed range"
            )
        if self.replica_store is not None:
            return self._replica_sync_page(value, doc_ids, max_bytes)
        fingerprint = self._cursor_fingerprint(value, doc_ids)
        offset = 0
        raw_cursor = value.get("cursor")
        if raw_cursor is not None:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(str(raw_cursor) + "=="))
                if decoded.get("fingerprint") != fingerprint or not isinstance(
                    decoded.get("offset"), int
                ):
                    raise ValueError
                offset = int(decoded["offset"])
            except Exception as exc:
                raise StoreError(
                    "bad-cursor", "cursor does not match this sync request"
                ) from exc
        if offset == 0:
            manifests: list[Mapping[str, Any]] = []
            meta_probe = self.store.export_since(
                "meta",
                _unb64(vectors.get("meta", "")),
                cursor=None,
                max_bytes=ORGFS_PAGE_BYTES,
            )
            # A retirement cannot be interpreted until the requester has its
            # owner-written meta prefix.  Send that prefix first; the next
            # round may then return the replacement snapshot manifest.
            if not meta_probe.envelopes:
                for doc_id in doc_ids:
                    probe = self.store.export_since(
                        doc_id,
                        _unb64(vectors.get(doc_id, "")),
                        cursor=None,
                        max_bytes=ORGFS_PAGE_BYTES,
                    )
                    manifests.extend(probe.snapshots)
            if manifests:
                response = _json_bytes(
                    {
                        "schemaVersion": 1,
                        "type": "orgfs-sync-snapshot",
                        "supplier": self.node_id,
                        "manifests": list(manifests),
                        "next": None,
                    }
                )
                if len(response) > max_bytes:
                    raise StoreError(
                        "too-large", "snapshot manifest page exceeds maxBytes"
                    )
                return response
        missing = self._flatten_missing(doc_ids, vectors)
        if offset < 0 or offset > len(missing):
            raise StoreError("bad-cursor", "cursor is outside the result set")
        encoded_envelopes: list[str] = []
        index = offset
        while index < len(missing):
            trial = [*encoded_envelopes, _b64(missing[index])]
            next_value = (
                base64.urlsafe_b64encode(
                    _json_bytes({"fingerprint": fingerprint, "offset": index + 1})
                )
                .decode("ascii")
                .rstrip("=")
                if index + 1 < len(missing)
                else None
            )
            response = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-sync-page",
                    "supplier": self.node_id,
                    "envelopes": trial,
                    "next": next_value,
                }
            )
            if len(response) > max_bytes:
                if not encoded_envelopes:
                    raise StoreError("too-large", "one envelope cannot fit in maxBytes")
                break
            encoded_envelopes = trial
            index += 1
        next_cursor = (
            base64.urlsafe_b64encode(
                _json_bytes({"fingerprint": fingerprint, "offset": index})
            )
            .decode("ascii")
            .rstrip("=")
            if index < len(missing)
            else None
        )
        response = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync-page",
                "supplier": self.node_id,
                "envelopes": encoded_envelopes,
                "next": next_cursor,
            }
        )
        if len(response) > max_bytes:
            raise StoreError("too-large", "sync page exceeds maxBytes")
        return response

    def _replica_sync_page(
        self, value: Mapping[str, Any], doc_ids: tuple[str, ...], max_bytes: int
    ) -> bytes:
        if self.replica_store is None:
            raise StoreError("invalid-argument", "replica store is unavailable")
        for doc_id in doc_ids:
            if doc_id != "meta" and self.store.retired(doc_id) is not None:
                # Ask the replica engine to construct the frozen error details;
                # the mesh handler below preserves its OrgFsError code.
                self.replica_store.serve_sync_page((doc_id,), limit=1)
        fingerprint = self._cursor_fingerprint(value, doc_ids)
        after_key: str | None = None
        cursor = value.get("cursor")
        if cursor is not None:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(str(cursor) + "=="))
                decoded_after = decoded.get("afterKey")
                if decoded.get("fingerprint") != fingerprint or not isinstance(
                    decoded_after, str
                ):
                    raise ValueError
                after_key = decoded_after
            except Exception as exc:
                raise StoreError(
                    "bad-cursor", "cursor does not match this sync request"
                ) from exc
        rows = self.replica_store.serve_sync_page(
            doc_ids,
            after_key=after_key,
            limit=ORGFS_REPLICA_SYNC_SCAN_ROWS + 1,
        )
        more_backend = len(rows) > ORGFS_REPLICA_SYNC_SCAN_ROWS
        rows = rows[:ORGFS_REPLICA_SYNC_SCAN_ROWS]
        vectors = value.get("versionVectors", {})
        selected: list[str] = []
        last_scanned_key = after_key
        more = more_backend

        def encode_cursor(key: str) -> str:
            return (
                base64.urlsafe_b64encode(
                    _json_bytes({"fingerprint": fingerprint, "afterKey": key})
                )
                .decode("ascii")
                .rstrip("=")
            )

        for index, (key, envelope) in enumerate(rows):
            parts = key.split("/")
            doc_id, writer, sequence = parts[2], parts[3], int(parts[4])
            _decode_update_header(envelope, space_id=self.store.space_id)
            version = self.store.commit_version(doc_id, writer, sequence)
            requested = _unb64(vectors.get(doc_id, ""))
            if version is not None and self.store._covered(version, requested):
                last_scanned_key = key
                continue
            trial = [*selected, _b64(envelope)]
            candidate_more = index + 1 < len(rows) or more_backend
            response = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-sync-page",
                    "supplier": self.node_id,
                    "envelopes": trial,
                    "next": encode_cursor(key) if candidate_more else None,
                }
            )
            if len(response) > max_bytes:
                if not selected:
                    raise StoreError("too-large", "one envelope cannot fit in maxBytes")
                more = True
                break
            selected = trial
            last_scanned_key = key
        next_cursor = None
        if more:
            if last_scanned_key is None:
                raise StoreError("bad-cursor", "replica sync made no cursor progress")
            next_cursor = encode_cursor(last_scanned_key)
        return _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync-page",
                "supplier": self.node_id,
                "envelopes": selected,
                "next": next_cursor,
            }
        )

    def _frontier_reply(self, value: Mapping[str, Any]) -> bytes:
        if str(value.get("spaceId")) != self.store.space_id:
            raise StoreError("unknown-space", "frontier requested another space")
        if not self._requester_allowed(value):
            raise StoreError("not-a-member", "requester is not a space member")
        return _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-frontier-reply",
                "supplier": self.node_id,
                "durable": (
                    self.replica_store.durable()
                    if self.replica_store is not None
                    else self.durable
                ),
                "frontiers": {
                    doc_id: _b64(self.store.frontier(doc_id))
                    for doc_id in self.store.document_ids()
                },
            }
        )

    def _handle_sync(
        self, _selector: str, payload: bytes | None
    ) -> Iterable[tuple[str, bytes]]:
        key = self._keys.orgfs_sync(self.store.space_id, self.node_id)
        try:
            value = _decode_request(payload)
            kind = value.get("type")
            if kind == "orgfs-sync":
                response = self._sync_page(value)
            elif kind == "orgfs-frontier":
                response = self._frontier_reply(value)
            else:
                raise StoreError("invalid-argument", "unknown sync query type")
        except (StoreError, OrgFsError) as exc:
            response = _error(exc.code, str(exc), **exc.details)
        except Exception as exc:
            self._log("error", "orgfs.query.failed", detail=str(exc))
            response = _error("invalid-argument", str(exc))
        return ((key, response),)

    def _handle_log_range(
        self, _selector: str, payload: bytes | None
    ) -> Iterable[tuple[str, bytes]]:
        value: dict[str, Any] = {}

        def error_key() -> str:
            doc_id = value.get("docId")
            writer = value.get("writer")
            after = value.get("after")
            if not isinstance(doc_id, str) or not doc_id:
                doc_id = "meta"
            if not isinstance(writer, str) or not writer:
                writer = "error"
            seq = after + 1 if type(after) is int and after >= 0 else 0
            try:
                return self._keys.orgfs_log(self.store.space_id, doc_id, writer, seq)
            except (TypeError, ValueError):
                return self._keys.orgfs_log(self.store.space_id, "meta", "error", 0)

        try:
            value = _decode_request(payload)
            if (
                value.get("type") != "orgfs-log-range"
                or str(value.get("spaceId")) != self.store.space_id
            ):
                raise StoreError("invalid-argument", "invalid log-range request")
            doc_id = str(value.get("docId", ""))
            writer = str(value.get("writer", ""))
            after = value.get("after")
            limit = value.get("limit", ORGFS_LOG_RANGE_LIMIT)
            if self.replica_store is not None:
                rows = self.replica_store.serve_log_range(
                    doc_id, writer, after=after, limit=limit
                )
                return tuple(
                    (
                        self._keys.orgfs_log(
                            self.store.space_id,
                            doc_id,
                            writer,
                            int(key.rsplit("/", 1)[1]),
                        ),
                        envelope,
                    )
                    for key, envelope in rows
                )
            records = self.store.log_range(doc_id, writer, after=after, limit=limit)
            return tuple(
                (
                    self._keys.orgfs_log(
                        self.store.space_id, doc_id, writer, record.seq
                    ),
                    record.envelope_bytes,
                )
                for record in records
            )
        except (StoreError, OrgFsError) as exc:
            return ((error_key(), _error(exc.code, str(exc), **exc.details)),)
        except Exception as exc:
            return ((error_key(), _error("invalid-argument", str(exc))),)

    def _handle_blob(
        self, selector: str, payload: bytes | None
    ) -> Iterable[tuple[str, bytes]]:
        if self.replica_store is not None:
            try:
                value = decode_blob_request(payload or b"")
                if value["spaceId"] != self.store.space_id:
                    raise StoreError("unknown-space", "blob requested another space")
                data = self.replica_store.serve_blob_chunk(
                    str(value["digest"]),
                    offset=int(value["offset"]),
                    length=int(value["length"]),
                )
                total_value = self.replica_store.backend.get(f"blob/{value['digest']}")
                if total_value is None:
                    raise OrgFsError(
                        "unknown-blob",
                        {
                            "digest": str(value["digest"]),
                            "spaceId": self.store.space_id,
                        },
                    )
                return (
                    (
                        selector,
                        encode_chunk(
                            str(value["digest"]),
                            int(value["offset"]),
                            len(total_value),
                            data,
                        ),
                    ),
                )
            except Exception as exc:
                return (
                    (
                        selector,
                        _error(
                            str(getattr(exc, "code", "invalid-argument")),
                            str(exc),
                            **dict(getattr(exc, "details", {}) or {}),
                        ),
                    ),
                )
        if self.blob_store is None:
            return ((selector, _error("unknown-blob", "blob store is unavailable")),)
        return ((selector, self.blob_store.handle_request(payload or b"")),)

    def _install_sync_snapshot(
        self, supplier: str, manifest: Mapping[str, Any], *, timeout: float
    ) -> None:
        if (
            manifest.get("schemaVersion") != 1
            or manifest.get("type") != "orgfs-snapshot"
            or manifest.get("spaceId") != self.store.space_id
        ):
            raise StoreError("invalid-argument", "invalid snapshot manifest")
        doc_id = manifest.get("docId")
        snapshot_id = manifest.get("snapshotId")
        size_bytes = manifest.get("sizeBytes")
        if (
            not isinstance(doc_id, str)
            or not isinstance(snapshot_id, str)
            or type(size_bytes) is not int
            or size_bytes <= 0
        ):
            raise StoreError("invalid-argument", "snapshot manifest is incomplete")
        retirement = next(
            (
                record
                for record in self.store.retirement_records()
                if record.replacement_doc_id == doc_id
                and record.snapshot_id == snapshot_id
            ),
            None,
        )
        if retirement is None:
            raise StoreError(
                "snapshot-barrier", "snapshot has no committed retirement record"
            )
        body = self.fetch_blob(
            supplier, snapshot_id, timeout=timeout, size_hint=size_bytes
        )
        if hashlib.sha256(body).hexdigest() != snapshot_id:
            raise StoreError("invalid-argument", "snapshot body hash mismatch")
        self.store.install_replacement(
            retirement.old_physical_doc_id,
            retirement.replacement_doc_id,
            body,
        )
        if self._on_replacement is not None:
            self._on_replacement(
                retirement.old_physical_doc_id,
                retirement.replacement_doc_id,
                body,
            )

    def sync_from(
        self,
        supplier: str,
        *,
        timeout: float = 3.0,
        deadline_monotonic: float | None = None,
    ) -> int:
        with self._worker_lock:
            self._sync_active.add(supplier)
        try:
            return self._sync_from_once(
                supplier, timeout=timeout, deadline_monotonic=deadline_monotonic
            )
        finally:
            with self._worker_lock:
                self._sync_active.discard(supplier)

    def _sync_from_once(
        self,
        supplier: str,
        *,
        timeout: float,
        deadline_monotonic: float | None,
    ) -> int:
        if not self._supplier_allowed(supplier):
            raise StoreError("not-a-member", "supplier is not an online host")
        cursor: str | None = None
        applied = 0
        starting_vectors = {
            doc_id: _b64(self.store.frontier(doc_id))
            for doc_id in self.store.document_ids()
        }
        exhausted_pending: tuple[tuple[str, str, str], ...] | None = None
        while True:
            query_timeout = timeout
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    raise StoreError("no-holder-online", "sync deadline expired")
                query_timeout = min(query_timeout, remaining)
            request = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-sync",
                    "spaceId": self.store.space_id,
                    "docIds": None,
                    "versionVectors": starting_vectors,
                    "requester": {"node": self.node_id, "author": self.author},
                    "cursor": cursor,
                    "maxBytes": ORGFS_PAGE_BYTES,
                }
            )
            replies = self.session.query(
                self._keys.orgfs_sync(self.store.space_id, supplier),
                request,
                timeout=query_timeout,
            )
            if not replies:
                raise StoreError("no-holder-online", "supplier did not answer")
            value = _decode_request(replies[0].payload)
            if value.get("type") == "orgfs-error":
                raise StoreError(str(value.get("code")), str(value.get("message")))
            if value.get("type") == "orgfs-sync-snapshot":
                if value.get("supplier") != supplier:
                    raise StoreError(
                        "invalid-argument", "snapshot supplier does not match"
                    )
                manifests = value.get("manifests")
                if not isinstance(manifests, list) or not manifests:
                    raise StoreError(
                        "invalid-argument", "snapshot response has no manifests"
                    )
                for manifest in manifests:
                    if not isinstance(manifest, dict):
                        raise StoreError(
                            "invalid-argument", "snapshot manifest must be an object"
                        )
                    self._install_sync_snapshot(
                        supplier, manifest, timeout=query_timeout
                    )
                    applied += 1
                if value.get("next") is not None:
                    raise StoreError(
                        "invalid-argument", "snapshot response cannot be paginated"
                    )
                starting_vectors = {
                    doc_id: _b64(self.store.frontier(doc_id))
                    for doc_id in self.store.document_ids()
                }
                cursor = None
                continue
            if (
                value.get("type") != "orgfs-sync-page"
                or value.get("supplier") != supplier
            ):
                raise StoreError("invalid-argument", "invalid sync response")
            envelopes = value.get("envelopes")
            if not isinstance(envelopes, list):
                raise StoreError("invalid-argument", "sync envelopes must be an array")
            for encoded in envelopes:
                result = self.receive(_unb64(encoded), supplier=supplier)
                if result.status == "applied":
                    applied += 1
            cursor = value.get("next")
            if cursor is None:
                pending = tuple(
                    (
                        record.old_physical_doc_id,
                        record.replacement_doc_id,
                        record.snapshot_id,
                    )
                    for record in self.store.pending_replacements()
                )
                if pending:
                    if pending == exhausted_pending:
                        raise StoreError(
                            "snapshot-unavailable",
                            "supplier did not offer the pending replacement snapshot",
                            supplier=supplier,
                            pendingReplacementDocIds=tuple(
                                replacement for _old, replacement, _snapshot in pending
                            ),
                        )
                    exhausted_pending = pending
                    starting_vectors = {
                        doc_id: _b64(self.store.frontier(doc_id))
                        for doc_id in self.store.document_ids()
                    }
                    continue
                return applied

    def sync_log_range_from(
        self,
        supplier: str,
        *,
        doc_id: str,
        writer: str,
        after: int | None = None,
        limit: int = ORGFS_LOG_RANGE_LIMIT,
        timeout: float = 3.0,
    ) -> int:
        """Fetch and admit one immutable per-writer journal range."""

        if not self._supplier_allowed(supplier):
            raise StoreError("not-a-member", "supplier is not an online host")
        request = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-log-range",
                "spaceId": self.store.space_id,
                "docId": doc_id,
                "writer": writer,
                "after": after,
                "limit": limit,
            }
        )
        replies = self.session.query(
            self._keys.orgfs_log_writer(self.store.space_id, doc_id, writer),
            request,
            timeout=timeout,
            all_replies=True,
        )
        if not replies:
            raise StoreError("no-holder-online", "log-range supplier did not answer")
        decoded: list[tuple[int, bytes]] = []
        for reply in replies:
            try:
                value = _decode_request(reply.payload)
            except StoreError:
                value = None
            if isinstance(value, dict) and value.get("type") == "orgfs-error":
                raise StoreError(str(value.get("code")), str(value.get("message")))
            envelope = _decode_update_header(
                reply.payload, space_id=self.store.space_id
            )
            origin = envelope["origin"]
            if str(envelope["docId"]) != doc_id or str(origin["writer"]) != writer:
                raise StoreError(
                    "invalid-argument", "log-range reply does not match the request"
                )
            decoded.append((int(envelope["seq"]), reply.payload))
        applied = 0
        for _seq, envelope in sorted(decoded):
            result = self.receive(envelope, supplier=supplier)
            if result.status == "applied" and result.code != "duplicate":
                applied += 1
        return applied

    def probe(self, supplier: str, *, timeout: float = 3.0) -> None:
        request = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-frontier",
                "spaceId": self.store.space_id,
                "requester": {"node": self.node_id, "author": self.author},
            }
        )
        replies = self.session.query(
            self._keys.orgfs_sync(self.store.space_id, supplier),
            request,
            timeout=timeout,
        )
        if not replies:
            raise StoreError("no-holder-online", "supplier did not answer")
        value = _decode_request(replies[0].payload)
        if value.get("type") == "orgfs-error":
            raise StoreError(str(value.get("code")), str(value.get("message")))
        frontiers = value.get("frontiers")
        if value.get("type") != "orgfs-frontier-reply" or not isinstance(
            frontiers, dict
        ):
            raise StoreError("invalid-argument", "invalid frontier response")
        self.store.record_holder_frontier(
            str(value.get("supplier")),
            bool(value.get("durable")),
            {str(doc_id): _unb64(frontier) for doc_id, frontier in frontiers.items()},
        )

    def fetch_blob(
        self,
        supplier: str,
        digest: str,
        *,
        timeout: float = 3.0,
        size_hint: int | None = None,
    ) -> bytes:
        if not self._supplier_allowed(supplier):
            raise StoreError("not-a-member", "blob supplier is not an online host")
        chunks: list[bytes] = []
        offset = 0
        total: int | None = None
        while total is None or offset < total:
            replies = self.session.query(
                self._keys.orgfs_blob(digest),
                encode_request(
                    self.store.space_id,
                    digest,
                    offset,
                    (
                        min(ORGFS_BLOB_CHUNK_BYTES, size_hint)
                        if total is None and type(size_hint) is int and size_hint > 0
                        else None
                        if total is None
                        else min(ORGFS_BLOB_CHUNK_BYTES, total - offset)
                    ),
                ),
                timeout=timeout,
                all_replies=True,
            )
            if not replies:
                raise StoreError("blob-unavailable", "blob supplier did not answer")
            chosen: bytes | None = None
            errors: list[dict[str, Any]] = []
            for reply in replies:
                value = _decode_request(reply.payload)
                if value.get("type") == "orgfs-error":
                    errors.append(value)
                    continue
                if value.get("type") == "orgfs-blob-chunk":
                    chosen = reply.payload
                    break
            if chosen is None:
                if errors:
                    raise StoreError(
                        str(errors[0].get("code")), str(errors[0].get("message"))
                    )
                raise StoreError("blob-unavailable", "no holder returned a blob chunk")
            try:
                chunk = decode_chunk(chosen)
            except (BlobError, ValueError) as exc:
                raise StoreError("blob-unavailable", str(exc)) from exc
            total = int(chunk["total"])
            chunks.append(chosen)
            offset += int(chunk["length"])
            if total == 0:
                break
        try:
            return assemble(digest, chunks)
        except BlobError as exc:
            raise StoreError("blob-unavailable", str(exc)) from exc

    def close(self) -> None:
        with self._worker_lock:
            self._closed = True
        for registration in reversed(self._registrations):
            registration.close()
        self._registrations.clear()
        self._worker_executor.shutdown(wait=True, cancel_futures=True)
        with self._worker_lock:
            self._blob_fetch_pending.clear()
            self._blob_fetch_pending_count = 0
            self._sync_pending.clear()


__all__ = [
    "ORGFS_ENVELOPE_BYTES",
    "ORGFS_BLOB_FETCH_QUEUE_LIMIT",
    "ORGFS_LOG_RANGE_LIMIT",
    "ORGFS_PAGE_BYTES",
    "ORGFS_SYNC_QUEUE_LIMIT",
    "OrgFsMesh",
]
