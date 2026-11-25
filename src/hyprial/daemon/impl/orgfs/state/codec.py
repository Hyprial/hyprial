# Decision record (C1 state store, 2026-11-25):
# - Byte formats are those of design section 2.2: 8-byte magic, then for delta
#   records and the batch body ``u32 length (big endian) + u32 CRC32 + payload``.
#   CRC32 is ``zlib.crc32`` (IEEE), the same checksum the rest of the repository
#   uses for framed data.
# - A delta scan stops at the first record whose header is short, whose length
#   runs past the end of the file, or whose CRC does not match; everything from
#   that offset on is the torn tail (section 2.3).  A file shorter than the magic
#   that is a prefix of it is a torn creation and reads as an empty delta.  Any
#   other magic is foreign data and is refused instead of being overwritten.
# - Merging uses a throwaway ``pycrdt.Doc``: it only applies updates and exports,
#   never produces operations, and keeps the default GC so collected content is
#   not carried into the merged update.  ``pycrdt.merge_updates`` was measured to
#   keep deleted content (599 vs 492 bytes on a small edit history), so it is not
#   used.  A one-part input is returned unchanged.
# - The batch body is JSON ``{"appends": [[docId, b64], ...], "deletes": [...]}``.
#   A body whose CRC is right but whose shape is wrong was written by something
#   other than this store; it is reported as corruption, not discarded.
"""Encoders and decoders for state, delta and batch files (design 2.2)."""

from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass

from pycrdt import Doc

from hyprial.daemon.impl.orgfs.state.interface import DIRECTORY_DOC_IDS, Batch

STATE_MAGIC = b"ORGFSST3"
DELTA_MAGIC = b"ORGFSDL3"
BATCH_MAGIC = b"ORGFSBT3"
MAGIC_BYTES = 8
_HEADER = struct.Struct(">II")
HEADER_BYTES = _HEADER.size


class CorruptStateFile(ValueError):
    """A state, delta or batch file holds data this store did not write."""


def encode_record(update: bytes) -> bytes:
    return _HEADER.pack(len(update), zlib.crc32(update)) + update


@dataclass(frozen=True, slots=True)
class DeltaScan:
    """Valid records of a delta file and where the valid prefix ends."""

    records: tuple[bytes, ...]
    valid_bytes: int
    file_bytes: int
    has_magic: bool

    @property
    def torn(self) -> bool:
        return self.valid_bytes != self.file_bytes

    @property
    def payload_bytes(self) -> int:
        return self.valid_bytes - MAGIC_BYTES if self.has_magic else 0


def scan_delta(raw: bytes) -> DeltaScan:
    if len(raw) < MAGIC_BYTES:
        if DELTA_MAGIC.startswith(raw):
            return DeltaScan((), 0, len(raw), False)
        raise CorruptStateFile("delta file has a foreign header")
    if raw[:MAGIC_BYTES] != DELTA_MAGIC:
        raise CorruptStateFile("delta file has a foreign header")
    records: list[bytes] = []
    offset = MAGIC_BYTES
    while offset + HEADER_BYTES <= len(raw):
        length, crc = _HEADER.unpack_from(raw, offset)
        start = offset + HEADER_BYTES
        end = start + length
        if end > len(raw):
            break
        payload = raw[start:end]
        if zlib.crc32(payload) != crc:
            break
        records.append(payload)
        offset = end
    return DeltaScan(tuple(records), offset, len(raw), True)


def decode_state(raw: bytes) -> bytes:
    if raw[:MAGIC_BYTES] != STATE_MAGIC:
        raise CorruptStateFile("state file has a foreign header")
    return raw[MAGIC_BYTES:]


def encode_state(update: bytes) -> bytes:
    return STATE_MAGIC + update


def merge(parts: Iterable[bytes]) -> bytes | None:
    """One update equivalent to applying ``parts`` in order, or None when empty."""

    chunks = list(parts)
    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]
    doc = Doc()
    for chunk in chunks:
        doc.apply_update(chunk)
    return doc.get_update()


def encode_batch(batch: Batch) -> bytes:
    body = json.dumps(
        {
            "appends": [
                [doc_id, base64.b64encode(update).decode("ascii")] for doc_id, update in batch.appends
            ],
            "deletes": list(batch.deletes),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return BATCH_MAGIC + _HEADER.pack(len(body), zlib.crc32(body)) + body


def decode_batch(raw: bytes) -> Batch | None:
    """The batch in ``raw``; None when the file was torn before its rename."""

    if len(raw) < MAGIC_BYTES + HEADER_BYTES or raw[:MAGIC_BYTES] != BATCH_MAGIC:
        return None
    length, crc = _HEADER.unpack_from(raw, MAGIC_BYTES)
    body = raw[MAGIC_BYTES + HEADER_BYTES :]
    if len(body) != length or zlib.crc32(body) != crc:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
        appends = tuple(
            (_doc_id(doc_id), base64.b64decode(encoded.encode("ascii"), validate=True))
            for doc_id, encoded in value["appends"]
        )
        deletes = tuple(_doc_id(doc_id) for doc_id in value["deletes"])
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise CorruptStateFile("batch.pending has a valid checksum but an unknown shape") from exc
    if not isinstance(value, dict) or set(value) != {"appends", "deletes"}:
        raise CorruptStateFile("batch.pending has a valid checksum but an unknown shape")
    return Batch(appends=appends, deletes=deletes)


def _doc_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("document id must be a non-empty string")
    return value


def require_doc_id(doc_id: str) -> None:
    if not isinstance(doc_id, str) or not doc_id:
        raise ValueError("document id must be a non-empty string")


def validate_batch(batch: Batch) -> None:
    """Reject a malformed batch before anything is written."""

    for doc_id, update in batch.appends:
        require_doc_id(doc_id)
        if not isinstance(update, bytes | bytearray) or not update:
            raise ValueError(f"update for {doc_id!r} must be non-empty bytes")
    for doc_id in batch.deletes:
        require_doc_id(doc_id)
        if doc_id in DIRECTORY_DOC_IDS:
            raise ValueError(f"{doc_id!r} is a directory document and cannot be deleted")
