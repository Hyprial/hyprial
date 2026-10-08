from __future__ import annotations


from collections.abc import Mapping



import hashlib



import time

from typing import TYPE_CHECKING, Any








from hyprial.daemon.impl.orgfs.storage.blobs  import (
    ORGFS_BLOB_CHUNK_BYTES,
    BlobError,
    assemble,
    decode_chunk,
    encode_request)

from hyprial.daemon.impl.orgfs.storage.store  import (
    ORGFS_PAGE_BYTES,
    StoreError)


if TYPE_CHECKING:
    pass


from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_BLOB_FETCH_TIMEOUT_SECONDS, ORGFS_LOG_RANGE_LIMIT, _BLOB_ABSENCE_CODES, _b64, _decode_request, _decode_update_header, _elapsed_ms, _json_bytes, _reply_origin, _unb64

class MeshSync:
    """Responsibility methods on the sole OrgFsMesh state host.

    This class never constructs, copies, or persists an independent host.
    """

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
                for record in self._retirement_records()
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
        if self.space_authority is not None:
            self.space_authority.install_replacement(
                retirement.old_physical_doc_id,
                retirement.replacement_doc_id,
                body,
                author=self.author,
                actor=self.node_id,
            )
        else:
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
            imported = self._sync_from_once(
                supplier, timeout=timeout, deadline_monotonic=deadline_monotonic
            )
            # Anti-entropy makes no-op syncs routine: only an import is news.
            self._log(
                "info" if imported > 0 else "debug",
                "orgfs.sync.completed",
                spaceId=self.store.space_id,
                supplier=supplier,
                importedCount=imported,
            )
            return imported
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


    def _sync_from_once(
        self,
        supplier: str,
        *,
        timeout: float,
        deadline_monotonic: float | None,
    ) -> int:
        projection_attempted: dict[
            tuple[str, str], tuple[int, set[int]]
        ] = {}
        self._repair_projections(attempted=projection_attempted)
        if not self._supplier_allowed(supplier):
            raise StoreError(
                "not-a-member",
                "supplier is not an online host",
                reason="supplier-not-online",
            )
        cursor: str | None = None
        applied = 0
        starting_vectors = {
            doc_id: _b64(self._frontier(doc_id))
            for doc_id in self._document_ids()
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
                    doc_id: _b64(self._frontier(doc_id))
                    for doc_id in self._document_ids()
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
                result = self._receive(
                    _unb64(encoded),
                    supplier=supplier,
                    defer_blob_fetch=False,
                    projection_attempted=projection_attempted,
                )
                if result.status == "rejected" and result.code == "supplier-offline":
                    # Presence can change after the initial gate. Do not let
                    # join hydrate a partial page; reuse its bounded retry path.
                    raise StoreError(
                        "not-a-member",
                        "supplier went offline during sync",
                        reason="supplier-not-online",
                    )
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
                    for record in self._pending_replacements()
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
                        doc_id: _b64(self._frontier(doc_id))
                        for doc_id in self._document_ids()
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
        projection_attempted: dict[
            tuple[str, str], tuple[int, set[int]]
        ] = {}
        for _seq, envelope in sorted(decoded):
            result = self._receive(
                envelope,
                supplier=supplier,
                defer_blob_fetch=False,
                projection_attempted=projection_attempted,
            )
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
        holder_frontiers = {
            str(doc_id): _unb64(frontier) for doc_id, frontier in frontiers.items()
        }
        if self.space_authority is not None:
            self.space_authority.record_holder_frontier(
                str(value.get("supplier")),
                bool(value.get("durable")),
                holder_frontiers,
            )
        else:
            self.store.record_holder_frontier(
                str(value.get("supplier")),
                bool(value.get("durable")),
                holder_frontiers,
            )


    def fetch_blob(
        self,
        supplier: str,
        digest: str,
        *,
        timeout: float = ORGFS_BLOB_FETCH_TIMEOUT_SECONDS,
        size_hint: int | None = None,
    ) -> bytes:
        if not self._supplier_allowed(supplier):
            raise StoreError("not-a-member", "blob supplier is not an online host")
        chunks: list[bytes] = []
        offset = 0
        total: int | None = None
        started = time.monotonic()
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
                raise StoreError(
                    "blob-unavailable",
                    "blob supplier did not answer",
                    elapsedMs=_elapsed_ms(started),
                )
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
                answered = [_reply_origin(error) for error in errors]
                decisive = [
                    error
                    for error in errors
                    if str(error.get("code")) not in _BLOB_ABSENCE_CODES
                ]
                if decisive:
                    raise StoreError(
                        str(decisive[0].get("code")),
                        str(decisive[0].get("message")),
                        replies=answered,
                        elapsedMs=_elapsed_ms(started),
                    )
                raise StoreError(
                    "blob-unavailable",
                    "no holder returned a blob chunk",
                    replies=answered,
                    elapsedMs=_elapsed_ms(started),
                )
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
