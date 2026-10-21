from __future__ import annotations


from collections.abc import Mapping
from dataclasses import dataclass



import hashlib
import json
import os
from pathlib import Path
import tempfile
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


from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_BLOB_FETCH_TIMEOUT_SECONDS, ORGFS_LEGACY_SYNC_DOCUMENT_LIMIT, ORGFS_LOG_RANGE_LIMIT, _BLOB_ABSENCE_CODES, _b64, _decode_request, _decode_update_header, _elapsed_ms, _json_bytes, _reply_origin, _unb64


@dataclass(slots=True)
class _SyncProgress:
    started: float
    pages: int = 0
    request_bytes: int = 0
    response_bytes: int = 0

    def metrics(self) -> dict[str, int]:
        return {
            "pages": self.pages,
            "requestBytes": self.request_bytes,
            "responseBytes": self.response_bytes,
            "elapsedMs": _elapsed_ms(self.started),
        }

    def timeout(self, reason: str, message: str) -> StoreError:
        return StoreError(
            "no-holder-online", message, reason=reason, **self.metrics()
        )


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
        progress = _SyncProgress(time.monotonic())
        with self._worker_lock:
            self._sync_active.add(supplier)
        try:
            try:
                imported = self._sync_from_once(
                    supplier,
                    timeout=timeout,
                    deadline_monotonic=deadline_monotonic,
                    _progress=progress,
                )
            except Exception as exc:
                details = getattr(exc, "details", None)
                if isinstance(details, dict):
                    for key, value in progress.metrics().items():
                        details.setdefault(key, value)
                raise
            # Anti-entropy makes no-op syncs routine: only an import is news.
            self._log(
                "info" if imported > 0 else "debug",
                "orgfs.sync.completed",
                spaceId=self.store.space_id,
                supplier=supplier,
                importedCount=imported,
                **progress.metrics(),
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


    def _sync_query(
        self,
        supplier: str,
        request: bytes,
        *,
        timeout: float,
        deadline_monotonic: float | None,
        progress: _SyncProgress,
    ) -> tuple[Mapping[str, Any], float]:
        query_timeout = timeout
        clipped_to_deadline = False
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise progress.timeout(
                    "deadline-expired-mid-sync",
                    "sync deadline expired between response pages",
                )
            clipped_to_deadline = remaining < query_timeout
            query_timeout = min(query_timeout, remaining)
        progress.request_bytes += len(request)
        replies = self.session.query(
            self._keys.orgfs_sync(self.store.space_id, supplier),
            request,
            timeout=query_timeout,
        )
        if not replies:
            if clipped_to_deadline:
                raise progress.timeout(
                    "deadline-expired-mid-sync",
                    "sync deadline expired while awaiting the final page",
                )
            raise progress.timeout("no-reply", "supplier returned no sync reply")
        progress.pages += 1
        progress.response_bytes += len(replies[0].payload)
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            raise progress.timeout(
                "reply-after-deadline",
                "supplier reply arrived after sync deadline",
            )
        return _decode_request(replies[0].payload), query_timeout


    def _sync_cursor_path(self, supplier: str) -> Path | None:
        root = getattr(self.store, "root", None)
        if root is None:
            return None
        name = hashlib.sha256(supplier.encode("utf-8")).hexdigest()
        return Path(root) / "sync-cursors" / f"{name}.json"


    def _load_sync_cursor(self, supplier: str) -> str | None:
        path = self._sync_cursor_path(supplier)
        if path is None or not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != 1
            or value.get("supplier") != supplier
            or not isinstance(value.get("cursor"), str)
        ):
            return None
        return value["cursor"]


    def _save_sync_cursor(self, supplier: str, cursor: str | None) -> None:
        path = self._sync_cursor_path(supplier)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if cursor is None:
            path.unlink(missing_ok=True)
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".sync-cursor-", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(
                        _json_bytes(
                            {
                                "schemaVersion": 1,
                                "supplier": supplier,
                                "cursor": cursor,
                            }
                        )
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


    @staticmethod
    def _remote_error(value: Mapping[str, Any]) -> StoreError:
        details = value.get("details")
        return StoreError(
            str(value.get("code")),
            str(value.get("message")),
            **(dict(details) if isinstance(details, Mapping) else {}),
        )


    def _sync_envelope_known(self, envelope: bytes) -> bool:
        header = _decode_update_header(envelope, space_id=self.store.space_id)
        origin = header["origin"]
        existing = self._commit_envelope(
            str(header["docId"]), str(origin["writer"]), int(header["seq"])
        )
        return existing == envelope


    def _apply_sync_envelopes(
        self,
        value: Mapping[str, Any],
        *,
        supplier: str,
        projection_attempted: dict[tuple[str, str], tuple[int, set[int]]],
    ) -> int:
        envelopes = value.get("envelopes")
        if not isinstance(envelopes, list):
            raise StoreError("invalid-argument", "sync envelopes must be an array")
        applied = 0
        for encoded in envelopes:
            envelope = _unb64(encoded)
            if self._sync_envelope_known(envelope):
                continue
            result = self._receive(
                envelope,
                supplier=supplier,
                defer_blob_fetch=False,
                projection_attempted=projection_attempted,
            )
            if result.status == "rejected" and result.code == "supplier-offline":
                raise StoreError(
                    "not-a-member",
                    "supplier went offline during sync",
                    reason="supplier-not-online",
                )
            if result.status == "applied" and result.code != "duplicate":
                applied += 1
        return applied


    def _stream_sync_from(
        self,
        supplier: str,
        *,
        timeout: float,
        deadline_monotonic: float | None,
        progress: _SyncProgress,
        projection_attempted: dict[tuple[str, str], tuple[int, set[int]]],
    ) -> int | None:
        cursor = self._load_sync_cursor(supplier)
        applied = 0
        while True:
            request = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-sync-stream",
                    "spaceId": self.store.space_id,
                    "requester": {"node": self.node_id, "author": self.author},
                    "cursor": cursor,
                    "maxBytes": ORGFS_PAGE_BYTES,
                }
            )
            try:
                value, _query_timeout = self._sync_query(
                    supplier,
                    request,
                    timeout=timeout,
                    deadline_monotonic=deadline_monotonic,
                    progress=progress,
                )
            except Exception as exc:
                details = getattr(exc, "details", None)
                if isinstance(details, dict):
                    details.setdefault("appliedCount", applied)
                raise
            if value.get("type") == "orgfs-error":
                if str(value.get("code")) == "invalid-argument":
                    self._save_sync_cursor(supplier, None)
                    return None
                raise self._remote_error(value)
            if value.get("type") in {"orgfs-sync-page", "orgfs-sync-snapshot"}:
                self._save_sync_cursor(supplier, None)
                return None
            if (
                value.get("type") != "orgfs-sync-stream-page"
                or value.get("supplier") != supplier
            ):
                raise StoreError("invalid-argument", "invalid stream sync response")
            applied += self._apply_sync_envelopes(
                value,
                supplier=supplier,
                projection_attempted=projection_attempted,
            )
            raw_cursor = value.get("next")
            if raw_cursor is not None and not isinstance(raw_cursor, str):
                raise StoreError("invalid-argument", "invalid stream sync cursor")
            cursor = raw_cursor
            self._save_sync_cursor(supplier, cursor)
            if cursor is None:
                return applied


    def _legacy_sync_from(
        self,
        supplier: str,
        *,
        timeout: float,
        deadline_monotonic: float | None,
        progress: _SyncProgress,
        projection_attempted: dict[tuple[str, str], tuple[int, set[int]]],
        doc_ids: tuple[str, ...] | None = None,
    ) -> int:
        local = set(self._document_ids())
        selected = tuple(local) if doc_ids is None else doc_ids
        vectors = {
            doc_id: _b64(self._frontier(doc_id))
            for doc_id in selected
            if doc_id in local
        }
        cursor: str | None = None
        applied = 0
        while True:
            request = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-sync",
                    "spaceId": self.store.space_id,
                    "docIds": doc_ids,
                    "versionVectors": vectors,
                    "requester": {"node": self.node_id, "author": self.author},
                    "cursor": cursor,
                    "maxBytes": ORGFS_PAGE_BYTES,
                }
            )
            if len(request) > ORGFS_PAGE_BYTES:
                raise StoreError(
                    "sync-peer-incompatible",
                    "bounded legacy sync request exceeds the wire limit",
                    reason="legacy-request-too-large",
                    requestBytes=len(request),
                    limit=ORGFS_PAGE_BYTES,
                )
            value, query_timeout = self._sync_query(
                supplier,
                request,
                timeout=timeout,
                deadline_monotonic=deadline_monotonic,
                progress=progress,
            )
            if value.get("type") == "orgfs-error":
                raise self._remote_error(value)
            if value.get("type") == "orgfs-sync-snapshot":
                manifests = value.get("manifests")
                if value.get("supplier") != supplier or not isinstance(manifests, list):
                    raise StoreError("invalid-argument", "invalid snapshot response")
                for manifest in manifests:
                    if not isinstance(manifest, dict):
                        raise StoreError(
                            "invalid-argument", "snapshot manifest must be an object"
                        )
                    self._install_sync_snapshot(
                        supplier, manifest, timeout=query_timeout
                    )
                    applied += 1
                vectors = {
                    doc_id: _b64(self._frontier(doc_id))
                    for doc_id in selected
                    if doc_id in set(self._document_ids())
                }
                cursor = None
                continue
            if (
                value.get("type") != "orgfs-sync-page"
                or value.get("supplier") != supplier
            ):
                raise StoreError("invalid-argument", "invalid legacy sync response")
            applied += self._apply_sync_envelopes(
                value,
                supplier=supplier,
                projection_attempted=projection_attempted,
            )
            raw_cursor = value.get("next")
            if raw_cursor is not None and not isinstance(raw_cursor, str):
                raise StoreError("invalid-argument", "invalid legacy sync cursor")
            cursor = raw_cursor
            if cursor is None:
                return applied


    def _sync_from_once(
        self,
        supplier: str,
        *,
        timeout: float,
        deadline_monotonic: float | None,
        _progress: _SyncProgress | None = None,
    ) -> int:
        progress = _progress or _SyncProgress(time.monotonic())
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
        if not self._supplier_member_allowed(supplier):
            raise StoreError(
                "not-a-member",
                "supplier's device owner is not a current member",
                reason="supplier-not-member",
            )
        applied = self._stream_sync_from(
            supplier,
            timeout=timeout,
            deadline_monotonic=deadline_monotonic,
            progress=progress,
            projection_attempted=projection_attempted,
        )
        if applied is None:
            local_count = len(self._document_ids())
            if local_count > ORGFS_LEGACY_SYNC_DOCUMENT_LIMIT:
                self._log(
                    "warn",
                    "orgfs.sync.legacy.skipped",
                    reason="legacy-peer-large-space",
                    supplier=supplier,
                    spaceId=self.store.space_id,
                    documentCount=local_count,
                    limit=ORGFS_LEGACY_SYNC_DOCUMENT_LIMIT,
                )
                raise StoreError(
                    "sync-peer-incompatible",
                    "legacy supplier fallback is disabled for a large space",
                    reason="legacy-peer-large-space",
                    documentCount=local_count,
                    limit=ORGFS_LEGACY_SYNC_DOCUMENT_LIMIT,
                )
            bounded_docs = tuple(dict.fromkeys(("meta", *self._document_ids())))
            self._log(
                "info",
                "orgfs.sync.legacy.bounded",
                reason="legacy-peer-bounded-known-documents",
                supplier=supplier,
                spaceId=self.store.space_id,
                documentCount=len(bounded_docs),
            )
            applied = self._legacy_sync_from(
                supplier,
                timeout=timeout,
                deadline_monotonic=deadline_monotonic,
                progress=progress,
                projection_attempted=projection_attempted,
                doc_ids=bounded_docs,
            )
        pending = tuple(self._pending_replacements())
        if not pending:
            return applied
        replacements = tuple(record.replacement_doc_id for record in pending)
        applied += self._legacy_sync_from(
            supplier,
            timeout=timeout,
            deadline_monotonic=deadline_monotonic,
            progress=progress,
            projection_attempted=projection_attempted,
            doc_ids=tuple(dict.fromkeys(("meta", *replacements))),
        )
        remaining = tuple(self._pending_replacements())
        if remaining:
            raise StoreError(
                "snapshot-unavailable",
                "supplier did not offer the pending replacement snapshot",
                supplier=supplier,
                pendingReplacementDocIds=tuple(
                    record.replacement_doc_id for record in remaining
                ),
            )
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
        if not self._supplier_member_allowed(supplier):
            raise StoreError(
                "not-a-member",
                "supplier's device owner is not a current member",
                reason="supplier-not-member",
            )
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
        if not self._supplier_member_allowed(supplier):
            raise StoreError(
                "not-a-member",
                "supplier's device owner is not a current member",
                reason="supplier-not-member",
            )
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
