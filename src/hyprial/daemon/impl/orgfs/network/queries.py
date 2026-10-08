from __future__ import annotations

import base64

from collections.abc import Iterable, Mapping



import hashlib

import json



from typing import TYPE_CHECKING, Any







from hyprial.daemon.impl.orgfs.api  import OrgFsError

from hyprial.daemon.impl.orgfs.storage.blobs  import (
    BlobError,
    decode_request as decode_blob_request,
    encode_chunk)

from hyprial.daemon.impl.orgfs.storage.store  import (
    ORGFS_PAGE_BYTES,
    CommitRecord,
    StoreError)

from hyprial.daemon.impl.orgfs.storage.space_authority  import _ReadStore

if TYPE_CHECKING:
    pass


from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_LOG_RANGE_LIMIT, ORGFS_REPLICA_SYNC_SCAN_ROWS, _READ_DIRECT, _b64, _decode_request, _decode_update_header, _error, _json_bytes, _unb64

class MeshQueries:
    """Responsibility methods on the sole OrgFsMesh state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _read(self, operation: _ReadStore) -> Any:
        authority = self.space_authority
        return authority.read(operation) if authority is not None else _READ_DIRECT


    def _document_ids(self) -> tuple[str, ...]:
        value = self._read(_ReadStore("document_ids"))
        return tuple(value) if value is not _READ_DIRECT else self.store.document_ids()


    def _frontier(self, doc_id: str) -> bytes:
        value = self._read(_ReadStore("frontier", doc_id=doc_id))
        return bytes(value) if value is not _READ_DIRECT else self.store.frontier(doc_id)


    def _writer_seq_watermarks(self, doc_id: str) -> dict[str, int]:
        value = self._read(_ReadStore("writer_seq_watermarks", doc_id=doc_id))
        watermarks = (
            self.store.writer_seq_watermarks(doc_id)
            if value is _READ_DIRECT
            else dict(value)
        )
        return {str(writer): int(seq) for writer, seq in watermarks.items()}


    def _export_since(
        self, doc_id: str, vv: bytes, *, cursor: str | None, max_bytes: int
    ) -> Any:
        value = self._read(
            _ReadStore(
                "export_since",
                doc_id=doc_id,
                vv=bytes(vv),
                cursor=cursor,
                max_bytes=max_bytes,
            )
        )
        return (
            value
            if value is not _READ_DIRECT
            else self.store.export_since(doc_id, vv, cursor=cursor, max_bytes=max_bytes)
        )


    def _member_mode(self, author: str) -> str | None:
        value = self._read(_ReadStore("member_mode", author=author))
        return value if value is not _READ_DIRECT else self.store.member_mode(author)


    def _retired(self, doc_id: str) -> Any:
        value = self._read(_ReadStore("retired", doc_id=doc_id))
        return value if value is not _READ_DIRECT else self.store.retired(doc_id)


    def _commit_version(self, doc_id: str, writer: str, seq: int) -> bytes | None:
        value = self._read(
            _ReadStore("commit_version", doc_id=doc_id, writer=writer, seq=seq)
        )
        return (
            self.store.commit_version(doc_id, writer, seq)
            if value is _READ_DIRECT
            else (None if value is None else bytes(value))
        )


    def _covered(self, version: bytes, vv: bytes) -> bool:
        value = self._read(_ReadStore("covered", version=version, vv=vv))
        return bool(value) if value is not _READ_DIRECT else self.store._covered(version, vv)


    def _log_range(
        self, doc_id: str, writer: str, *, after: int | None, limit: int
    ) -> tuple[CommitRecord, ...]:
        value = self._read(
            _ReadStore("log_range", doc_id=doc_id, writer=writer, after=after, limit=limit)
        )
        return tuple(value) if value is not _READ_DIRECT else self.store.log_range(
            doc_id, writer, after=after, limit=limit
        )


    def _retirement_records(self) -> tuple[Any, ...]:
        value = self._read(_ReadStore("retirement_records"))
        return tuple(value) if value is not _READ_DIRECT else self.store.retirement_records()


    def _pending_replacements(self) -> tuple[Any, ...]:
        value = self._read(_ReadStore("pending_replacements"))
        return tuple(value) if value is not _READ_DIRECT else self.store.pending_replacements()


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
                page = self._export_since(
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
            doc_ids = self._document_ids()
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
            meta_probe = self._export_since(
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
                    probe = self._export_since(
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
            if doc_id != "meta" and self._retired(doc_id) is not None:
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
            version = self._commit_version(doc_id, writer, sequence)
            requested = _unb64(vectors.get(doc_id, ""))
            if version is not None and self._covered(version, requested):
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
                    doc_id: _b64(self._frontier(doc_id))
                    for doc_id in self._document_ids()
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
            records = self._log_range(doc_id, writer, after=after, limit=limit)
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
                    raise OrgFsError("unknown-blob", {"digest": str(value["digest"])})
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
                return ((selector, self._blob_error(exc)),)
        if self.blob_store is None:
            return (
                (
                    selector,
                    self._blob_error(
                        StoreError("unknown-blob", "blob store is unavailable")
                    ),
                ),
            )
        try:
            return ((selector, self.blob_store.get_chunk(payload or b"")),)
        except BlobError as exc:
            return ((selector, self._blob_error(exc)),)
        except (TypeError, ValueError) as exc:
            return (
                (selector, self._blob_error(StoreError("invalid-argument", str(exc)))),
            )


    def _blob_error(self, exc: BaseException) -> bytes:
        # The blob key carries no node segment, so the answer itself must say
        # who answered and for which space.
        return _error(
            str(getattr(exc, "code", "invalid-argument")),
            str(exc),
            **{
                **dict(getattr(exc, "details", {}) or {}),
                "nodeId": self.node_id,
                "spaceId": self.store.space_id,
            },
        )
