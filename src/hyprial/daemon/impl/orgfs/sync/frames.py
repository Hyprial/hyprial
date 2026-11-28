# Decision record (C3, 2026-11-25, design §4.3, §4.4):
# - Every frame is JSON with schemaVersion 3.  A decoder validates the whole
#   frame (types, sizes, base64, state vectors, update decodability, doc id
#   shape, reply/request correlation) before returning anything, so a caller
#   never applies part of an invalid frame.  Unknown top-level keys are
#   refused: the version field, not tolerance, is the extension point.
# - The design defines no error reply.  A supplier that refuses a request
#   answers {"type":"sync-error", code, message} with the same correlation
#   fields as a reply; the requester turns it into a typed SyncFailure.
# - A broadcast whose encoded frame would exceed SYNC_ENVELOPE_BYTES is sent
#   without its body; the header (spaceId, node, docId) stays so the receiver
#   knows which peer to pull from (§4.4 lists only docId and vector).
# - Content doc ids must pass identity's protected-id parser, the same shape
#   check the journal protocol applied (network/protocol.py).
"""Protocol 3 sync request / reply and broadcast frame codecs."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Any

from hyprial.identity import parse_protected_directory_doc_id

from hyprial.daemon.impl.orgfs.sync.interface import (
    CATALOG_DOC_IDS,
    ORGFS_PAGE_BYTES,
    SYNC_BLOB_MAX_BYTES,
    SYNC_CONTENT_DOCUMENT_LIMIT,
    SYNC_DOC_ID_MAX_LENGTH,
    SYNC_ENVELOPE_BYTES,
    SYNC_MIN_PAGE_BYTES,
    SYNC_SCHEMA_VERSION,
)
from hyprial.daemon.impl.orgfs.sync.vectors import FrameError, check_update, decode_vector

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_KEYS = frozenset({"schemaVersion", "type", "spaceId", "round", "requester", "vectors", "maxBytes"})
_REPLY_KEYS = frozenset({"schemaVersion", "type", "spaceId", "round", "supplier", "updates", "vectors", "omitted"})
_ERROR_KEYS = frozenset({"schemaVersion", "type", "spaceId", "round", "supplier", "code", "message"})
_UPDATE_KEYS = frozenset({"schemaVersion", "type", "spaceId", "node", "docId", "update", "vector"})


@dataclass(frozen=True, slots=True)
class SyncRequest:
    space_id: str
    round: str
    requester_node: str
    requester_author: str
    vectors: tuple[tuple[str, bytes], ...]
    max_bytes: int


@dataclass(frozen=True, slots=True)
class ReplyEntry:
    doc_id: str
    vector: bytes
    update: bytes | None = None
    blob: str | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class SyncReply:
    space_id: str
    round: str
    supplier: str
    entries: tuple[ReplyEntry, ...]
    meta_vector: bytes
    tree_vector: bytes
    omitted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyncFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class UpdateFrame:
    space_id: str
    node: str
    doc_id: str
    vector: bytes
    update: bytes | None


def json_bytes(value: Mapping[str, Any], *, sort_keys: bool = True) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, separators=(",", ":")).encode("utf-8")


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object, what: str) -> bytes:
    if not isinstance(value, str):
        raise FrameError(f"{what} must be base64 text")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise FrameError(f"{what} is not valid base64") from exc


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > SYNC_DOC_ID_MAX_LENGTH:
        raise FrameError(f"{what} must be a non-empty string")
    if any(ord(char) < 0x20 for char in value):
        raise FrameError(f"{what} contains control characters")
    return value


def check_doc_id(value: object) -> str:
    doc_id = _text(value, "docId")
    try:
        parse_protected_directory_doc_id(doc_id)
    except ValueError as exc:
        raise FrameError("docId is not a valid protected document id") from exc
    return doc_id


def _vector(value: object, what: str) -> bytes:
    raw = _unb64(value, what)
    decode_vector(raw)
    return raw


def _object(payload: bytes | None, *, limit: int, keys: frozenset[str], kind: str) -> dict[str, Any]:
    if payload is None:
        raise FrameError(f"{kind} requires a payload")
    if len(payload) > limit:
        raise FrameError(f"{kind} exceeds {limit} bytes")
    try:
        value = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(f"{kind} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FrameError(f"{kind} must be a JSON object")
    if value.get("schemaVersion") != SYNC_SCHEMA_VERSION or type(value.get("schemaVersion")) is not int:
        raise FrameError(f"{kind} has an unsupported schemaVersion")
    if not set(value) <= keys:
        raise FrameError(f"{kind} has unknown fields")
    return value


def encode_request(request: SyncRequest) -> bytes:
    return json_bytes(
        {
            "schemaVersion": SYNC_SCHEMA_VERSION,
            "type": "sync",
            "spaceId": request.space_id,
            "round": request.round,
            "requester": {"node": request.requester_node, "author": request.requester_author},
            "vectors": {doc_id: b64(vector) for doc_id, vector in request.vectors},
            "maxBytes": request.max_bytes,
        },
        sort_keys=False,
    )


def decode_request(payload: bytes | None, *, space_id: str) -> SyncRequest:
    value = _object(payload, limit=ORGFS_PAGE_BYTES, keys=_REQUEST_KEYS, kind="sync request")
    if set(value) != _REQUEST_KEYS or value["type"] != "sync" or value["spaceId"] != space_id:
        raise FrameError("sync request header does not match this space")
    round_id = _text(value["round"], "round")
    requester = value["requester"]
    if not isinstance(requester, dict) or set(requester) != {"node", "author"}:
        raise FrameError("sync request requester must name node and author")
    node = _text(requester["node"], "requester.node")
    author = _text(requester["author"], "requester.author")
    vectors = value["vectors"]
    if not isinstance(vectors, dict) or any(doc_id not in vectors for doc_id in CATALOG_DOC_IDS):
        raise FrameError("sync request vectors must include meta and tree")
    if len(vectors) > len(CATALOG_DOC_IDS) + SYNC_CONTENT_DOCUMENT_LIMIT:
        raise FrameError("sync request names too many documents")
    ordered = ordered_ids(vectors)
    decoded = tuple((check_doc_id(doc_id), _vector(vectors[doc_id], "vector")) for doc_id in ordered)
    max_bytes = value["maxBytes"]
    if type(max_bytes) is not int or max_bytes < SYNC_MIN_PAGE_BYTES:
        raise FrameError("sync request maxBytes is invalid")
    return SyncRequest(space_id, round_id, node, author, decoded, max_bytes)


def entry_value(entry: ReplyEntry) -> dict[str, Any]:
    value: dict[str, Any] = {"docId": entry.doc_id, "vector": b64(entry.vector)}
    if entry.update is not None:
        value["update"] = b64(entry.update)
    else:
        value["blob"] = entry.blob
        value["size"] = entry.size
    return value


def encode_reply(reply: SyncReply) -> bytes:
    return json_bytes(
        {
            "schemaVersion": SYNC_SCHEMA_VERSION,
            "type": "sync-reply",
            "spaceId": reply.space_id,
            "round": reply.round,
            "supplier": reply.supplier,
            "updates": [entry_value(entry) for entry in reply.entries],
            "vectors": {"meta": b64(reply.meta_vector), "tree": b64(reply.tree_vector)},
            "omitted": list(reply.omitted),
        }
    )


def encode_error(*, space_id: str, round: str, supplier: str, code: str, message: str) -> bytes:
    return json_bytes(
        {
            "schemaVersion": SYNC_SCHEMA_VERSION,
            "type": "sync-error",
            "spaceId": space_id,
            "round": round,
            "supplier": supplier,
            "code": code,
            "message": message,
        }
    )


def _decode_entry(value: object, requested: frozenset[str]) -> ReplyEntry:
    if not isinstance(value, dict):
        raise FrameError("reply update must be an object")
    doc_id = check_doc_id(value.get("docId"))
    if doc_id not in requested:
        raise FrameError("reply carries a document that was not requested")
    vector = _vector(value.get("vector"), "vector")
    if set(value) == {"docId", "vector", "update"}:
        update = _unb64(value["update"], "update")
        check_update(update)
        return ReplyEntry(doc_id, vector, update=update)
    if set(value) == {"docId", "vector", "blob", "size"}:
        blob = value["blob"]
        size = value["size"]
        if not isinstance(blob, str) or _HEX_DIGEST.fullmatch(blob) is None:
            raise FrameError("reply blob must be a sha256 hex digest")
        if type(size) is not int or size <= 0 or size > SYNC_BLOB_MAX_BYTES:
            raise FrameError("reply blob size is invalid")
        return ReplyEntry(doc_id, vector, blob=blob, size=size)
    raise FrameError("reply update must carry exactly one body")


def decode_reply(
    payload: bytes | None,
    *,
    space_id: str,
    round: str,
    supplier: str,
    requested: frozenset[str],
) -> SyncReply | SyncFailure:
    value = _object(payload, limit=ORGFS_PAGE_BYTES, keys=_REPLY_KEYS | _ERROR_KEYS, kind="sync reply")
    if value.get("spaceId") != space_id or value.get("round") != round or value.get("supplier") != supplier:
        raise FrameError("sync reply does not answer this request")
    if value.get("type") == "sync-error":
        if set(value) != _ERROR_KEYS:
            raise FrameError("sync error reply is malformed")
        return SyncFailure(_text(value["code"], "code"), str(value["message"]))
    if value.get("type") != "sync-reply" or set(value) != _REPLY_KEYS:
        raise FrameError("sync reply is malformed")
    updates = value["updates"]
    if not isinstance(updates, list):
        raise FrameError("sync reply updates must be a list")
    entries = tuple(_decode_entry(item, requested) for item in updates)
    seen = [entry.doc_id for entry in entries]
    if len(set(seen)) != len(seen):
        raise FrameError("sync reply repeats a document")
    vectors = value["vectors"]
    if not isinstance(vectors, dict) or set(vectors) != set(CATALOG_DOC_IDS):
        raise FrameError("sync reply vectors must be exactly meta and tree")
    omitted = value["omitted"]
    if not isinstance(omitted, list):
        raise FrameError("sync reply omitted must be a list")
    omitted_ids = tuple(check_doc_id(doc_id) for doc_id in omitted)
    if (
        len(set(omitted_ids)) != len(omitted_ids)
        or not set(omitted_ids) <= requested
        or set(omitted_ids) & set(seen)
    ):
        raise FrameError("sync reply omitted list is invalid")
    return SyncReply(
        space_id,
        round,
        supplier,
        entries,
        _vector(vectors["meta"], "vector"),
        _vector(vectors["tree"], "vector"),
        omitted_ids,
    )


def encode_update(frame: UpdateFrame) -> bytes:
    value: dict[str, Any] = {
        "schemaVersion": SYNC_SCHEMA_VERSION,
        "type": "update",
        "spaceId": frame.space_id,
        "node": frame.node,
        "docId": frame.doc_id,
        "vector": b64(frame.vector),
    }
    if frame.update is not None and (len(frame.update) + 2) // 3 * 4 < SYNC_ENVELOPE_BYTES:
        value["update"] = b64(frame.update)
        encoded = json_bytes(value)
        if len(encoded) <= SYNC_ENVELOPE_BYTES:
            return encoded
        del value["update"]
    return json_bytes(value)


def decode_update(payload: bytes | None, *, space_id: str) -> UpdateFrame:
    value = _object(payload, limit=SYNC_ENVELOPE_BYTES, keys=_UPDATE_KEYS, kind="update frame")
    if value.get("type") != "update" or value.get("spaceId") != space_id:
        raise FrameError("update frame header does not match this space")
    if not {"node", "docId", "vector"} <= set(value):
        raise FrameError("update frame is incomplete")
    node = _text(value["node"], "node")
    doc_id = check_doc_id(value["docId"])
    vector = _vector(value["vector"], "vector")
    update = None
    if "update" in value:
        update = _unb64(value["update"], "update")
        check_update(update)
    return UpdateFrame(space_id, node, doc_id, vector, update)


def ordered_ids(doc_ids: Iterable[str]) -> list[str]:
    """Catalog first (meta, tree), then content in the given order."""

    ids = list(dict.fromkeys(doc_ids))
    return [doc_id for doc_id in CATALOG_DOC_IDS if doc_id in ids] + [
        doc_id for doc_id in ids if doc_id not in CATALOG_DOC_IDS
    ]


__all__ = [
    "FrameError",
    "ReplyEntry",
    "SyncFailure",
    "SyncReply",
    "SyncRequest",
    "UpdateFrame",
    "b64",
    "check_doc_id",
    "decode_reply",
    "decode_request",
    "decode_update",
    "encode_error",
    "encode_reply",
    "encode_request",
    "encode_update",
    "entry_value",
    "json_bytes",
    "ordered_ids",
]
