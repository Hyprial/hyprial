from __future__ import annotations

import base64
from bisect import bisect_right

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


from hyprial.daemon.impl.orgfs.network.protocol import ORGFS_LOG_RANGE_LIMIT, ORGFS_REPLICA_SYNC_SCAN_ROWS, ORGFS_SYNC_PAGE_DOCUMENT_LIMIT, _READ_DIRECT, _b64, _decode_request, _decode_update_header, _error, _json_bytes, _unb64

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


    def _commit_envelope(self, doc_id: str, writer: str, seq: int) -> bytes | None:
        value = self._read(
            _ReadStore("commit_envelope", doc_id=doc_id, writer=writer, seq=seq)
        )
        return (
            self.store.commit_envelope(doc_id, writer, seq)
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


    @staticmethod
    def _encode_sync_cursor(
        fingerprint: str, doc_index: int, export_cursor: str | None
    ) -> str:
        return (
            base64.urlsafe_b64encode(
                _json_bytes(
                    {
                        "fingerprint": fingerprint,
                        "docIndex": doc_index,
                        "exportCursor": export_cursor,
                    }
                )
            )
            .decode("ascii")
            .rstrip("=")
        )


    @staticmethod
    def _decode_sync_cursor(
        raw_cursor: object, fingerprint: str
    ) -> tuple[int, str | None]:
        if raw_cursor is None:
            return 0, None
        try:
            decoded = json.loads(base64.urlsafe_b64decode(str(raw_cursor) + "=="))
            doc_index = decoded.get("docIndex")
            export_cursor = decoded.get("exportCursor")
            if (
                decoded.get("fingerprint") != fingerprint
                or type(doc_index) is not int
                or doc_index < 0
                or (export_cursor is not None and not isinstance(export_cursor, str))
            ):
                raise ValueError
            return doc_index, export_cursor
        except Exception as exc:
            raise StoreError(
                "bad-cursor", "cursor does not match this sync request"
            ) from exc


    def _document_page(self, value: Mapping[str, Any]) -> bytes:
        if str(value.get("spaceId")) != self.store.space_id:
            raise StoreError("unknown-space", "sync requested another space")
        if not self._requester_allowed(value):
            raise StoreError("not-a-member", "requester is not a space member")
        max_bytes = value.get("maxBytes", ORGFS_PAGE_BYTES)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 0 < max_bytes <= ORGFS_PAGE_BYTES
        ):
            raise StoreError(
                "invalid-argument", "maxBytes is outside the allowed range"
            )
        raw_cursor = value.get("cursor")
        if raw_cursor is None:
            offset = 0
        elif type(raw_cursor) is int and raw_cursor >= 0:
            offset = raw_cursor
        else:
            raise StoreError("bad-cursor", "document cursor is invalid")
        known = self._document_ids()
        doc_ids = tuple(dict.fromkeys(("meta", *sorted(known))))
        if offset > len(doc_ids):
            raise StoreError("bad-cursor", "document cursor is outside the result set")
        selected: list[str] = []
        index = offset
        encoded_array_size = 2
        while index < len(doc_ids):
            encoded_size = len(
                json.dumps(doc_ids[index], ensure_ascii=False).encode("utf-8")
            )
            added = encoded_size + (1 if selected else 0)
            next_index = index + 1
            candidate_next = next_index if next_index < len(doc_ids) else None
            empty_candidate_size = len(
                _json_bytes(
                    {
                        "schemaVersion": 1,
                        "type": "orgfs-sync-documents-page",
                        "supplier": self.node_id,
                        "docIds": [],
                        "next": candidate_next,
                    }
                )
            )
            if empty_candidate_size + encoded_array_size + added - 2 > max_bytes:
                if not selected:
                    raise StoreError("too-large", "one document id cannot fit")
                break
            selected.append(doc_ids[index])
            encoded_array_size += added
            index = next_index
        response = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync-documents-page",
                "supplier": self.node_id,
                "docIds": selected,
                "next": index if index < len(doc_ids) else None,
            }
        )
        if len(response) > max_bytes:
            raise StoreError("too-large", "document page exceeds maxBytes")
        return response


    @staticmethod
    def _encode_stream_cursor(
        after_doc_id: str | None, doc_id: str | None, export_cursor: str | None
    ) -> str:
        return (
            base64.urlsafe_b64encode(
                _json_bytes(
                    {
                        "afterDocId": after_doc_id,
                        "docId": doc_id,
                        "exportCursor": export_cursor,
                    }
                )
            )
            .decode("ascii")
            .rstrip("=")
        )


    @staticmethod
    def _decode_stream_cursor(
        raw_cursor: object, doc_ids: tuple[str, ...]
    ) -> tuple[int, str | None, str | None]:
        if raw_cursor is None:
            return 0, None, None
        if not isinstance(raw_cursor, str):
            raise StoreError("bad-cursor", "stream cursor is invalid")
        try:
            decoded = json.loads(base64.urlsafe_b64decode(raw_cursor + "=="))
            after_doc_id = decoded.get("afterDocId")
            doc_id = decoded.get("docId")
            export_cursor = decoded.get("exportCursor")
            if any(
                value is not None and not isinstance(value, str)
                for value in (after_doc_id, doc_id, export_cursor)
            ):
                raise ValueError
            if doc_id is not None:
                if doc_id not in doc_ids or export_cursor is None:
                    raise ValueError
                return doc_ids.index(doc_id), after_doc_id, export_cursor
            if export_cursor is not None:
                raise ValueError
            if after_doc_id is None:
                return 0, None, None
            if after_doc_id == "meta":
                return 1, after_doc_id, None
            regular = doc_ids[1:]
            return 1 + bisect_right(regular, after_doc_id), after_doc_id, None
        except Exception as exc:
            raise StoreError("bad-cursor", "stream cursor is invalid") from exc


    def _stream_sync_page(self, value: Mapping[str, Any]) -> bytes:
        if str(value.get("spaceId")) != self.store.space_id:
            raise StoreError("unknown-space", "sync requested another space")
        if not self._requester_allowed(value):
            raise StoreError("not-a-member", "requester is not a space member")
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
            return self._replica_sync_page(
                {
                    **value,
                    "docIds": None,
                    "versionVectors": {},
                },
                tuple(dict.fromkeys(("meta", *self._document_ids()))),
                max_bytes,
                response_type="orgfs-sync-stream-page",
            )
        doc_ids = tuple(dict.fromkeys(("meta", *sorted(self._document_ids()))))
        index, after_doc_id, export_cursor = self._decode_stream_cursor(
            value.get("cursor"), doc_ids
        )
        selected: list[str] = []
        scanned = 0
        used = len(
            _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-sync-stream-page",
                    "supplier": self.node_id,
                    "envelopes": [],
                    "next": "x" * 512,
                }
            )
        )
        while index < len(doc_ids) and scanned < ORGFS_SYNC_PAGE_DOCUMENT_LIMIT:
            doc_id = doc_ids[index]
            remaining = max_bytes - used
            page = self._export_since(
                doc_id,
                b"",
                cursor=export_cursor,
                max_bytes=max(1, remaining - 1024),
            )
            encoded = [_b64(envelope) for envelope in page.envelopes]
            added = sum(len(item.encode("ascii")) + 3 for item in encoded)
            if used + added > max_bytes:
                if not selected:
                    raise StoreError("too-large", "one envelope cannot fit in maxBytes")
                break
            selected.extend(encoded)
            used += added
            if page.next is not None:
                export_cursor = page.next
                break
            after_doc_id = doc_id
            export_cursor = None
            index += 1
            scanned += 1
        next_cursor = None
        if index < len(doc_ids):
            next_cursor = self._encode_stream_cursor(
                after_doc_id,
                doc_ids[index] if export_cursor is not None else None,
                export_cursor,
            )
        response = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync-stream-page",
                "supplier": self.node_id,
                "envelopes": selected,
                "next": next_cursor,
            }
        )
        if len(response) > max_bytes:
            raise StoreError("too-large", "sync stream page exceeds maxBytes")
        return response


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
            return self._replica_sync_page(
                value,
                doc_ids,
                max_bytes,
                response_type="orgfs-sync-page",
            )
        fingerprint = self._cursor_fingerprint(value, doc_ids)
        doc_index, export_cursor = self._decode_sync_cursor(
            value.get("cursor"), fingerprint
        )
        if doc_index > len(doc_ids):
            raise StoreError("bad-cursor", "cursor is outside the document set")
        if doc_index == 0 and export_cursor is None:
            manifests: list[Mapping[str, Any]] = []
            meta_probe = self._export_since(
                "meta",
                _unb64(vectors.get("meta", "")),
                cursor=None,
                max_bytes=ORGFS_PAGE_BYTES,
            )
            # A retirement cannot be interpreted until the requester has its
            # owner-written meta prefix. Send that prefix first; only committed
            # replacement records can require a snapshot probe afterward.
            if not meta_probe.envelopes:
                replacement_ids = {
                    record.replacement_doc_id
                    for record in self._retirement_records()
                }
                for doc_id in doc_ids:
                    if doc_id not in replacement_ids:
                        continue
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
        while doc_index < len(doc_ids):
            doc_id = doc_ids[doc_index]
            vector = _unb64(vectors.get(doc_id, ""))
            if export_cursor is None and vector and vector == self._frontier(doc_id):
                doc_index += 1
                continue
            page = self._export_since(
                doc_id,
                vector,
                cursor=export_cursor,
                # Store paging already accounts for base64 envelope size; keep
                # only the outer response and cursor overhead out of its budget.
                max_bytes=max(1, max_bytes - 1024),
            )
            if not page.envelopes:
                doc_index += 1
                export_cursor = None
                continue
            next_doc_index = doc_index if page.next is not None else doc_index + 1
            next_cursor = (
                self._encode_sync_cursor(fingerprint, next_doc_index, page.next)
                if next_doc_index < len(doc_ids)
                else None
            )
            response = _json_bytes(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-sync-page",
                    "supplier": self.node_id,
                    "envelopes": [_b64(envelope) for envelope in page.envelopes],
                    "next": next_cursor,
                }
            )
            if len(response) > max_bytes:
                raise StoreError("too-large", "sync page exceeds maxBytes")
            return response
        response = _json_bytes(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync-page",
                "supplier": self.node_id,
                "envelopes": [],
                "next": None,
            }
        )
        if len(response) > max_bytes:
            raise StoreError("too-large", "sync page exceeds maxBytes")
        return response


    def _replica_sync_page(
        self,
        value: Mapping[str, Any],
        doc_ids: tuple[str, ...],
        max_bytes: int,
        *,
        response_type: str,
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
                    "type": response_type,
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
        response = _json_bytes(
            {
                "schemaVersion": 1,
                "type": response_type,
                "supplier": self.node_id,
                "envelopes": selected,
                "next": next_cursor,
            }
        )
        if len(response) > max_bytes:
            raise StoreError("too-large", "sync page exceeds maxBytes")
        return response


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
            elif kind == "orgfs-sync-stream":
                response = self._stream_sync_page(value)
            elif kind == "orgfs-sync-documents":
                response = self._document_page(value)
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
