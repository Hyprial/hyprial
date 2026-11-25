"""Frame codec: 4-byte big-endian header length, canonical JSON header, raw body.

Decisions:

- The header is canonical (sorted keys, compact separators, non-ASCII kept) so
  equal headers are equal bytes; the body is never base64.
- Every malformed frame raises ``TELEPORT_BAD_FRAME``.  Validation is strict
  about types (``bool`` is not an int, ``1.0`` is not version 1) because a
  lenient parser would let two implementations disagree on what is accepted.
- Business failures travel as ``ok:false`` reply frames, not transport errors.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.teleport.errors import (
    TELEPORT_OPS,
    TELEPORT_VERSION,
    TeleportError,
)

_LENGTH = struct.Struct(">I")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Frame:
    header: dict[str, Any]
    body: bytes


@dataclass(frozen=True, slots=True)
class ChunkRequest:
    operation_id: str
    offset: int
    total: int
    size: int
    stream_sha256: str
    chunk_sha256: str
    data: bytes


def _bad(message: str) -> TeleportError:
    return TeleportError("TELEPORT_BAD_FRAME", message)


def encode_frame(header: Mapping[str, Any], body: bytes = b"") -> bytes:
    encoded = json.dumps(
        dict(header), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return _LENGTH.pack(len(encoded)) + encoded + bytes(body)


def decode_frame(data: bytes) -> Frame:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise _bad("frame must be bytes")
    data = bytes(data)
    if len(data) < _LENGTH.size:
        raise _bad("frame shorter than its length prefix")
    (length,) = _LENGTH.unpack_from(data)
    end = _LENGTH.size + length
    if length == 0 or end > len(data):
        raise _bad("header length out of range")
    try:
        header = json.loads(data[_LENGTH.size : end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _bad("header is not valid UTF-8 JSON") from exc
    if not isinstance(header, dict):
        raise _bad("header must be a JSON object")
    version = header.get("v")
    if type(version) is not int or version != TELEPORT_VERSION:
        raise _bad("unsupported frame version")
    if header.get("op") not in TELEPORT_OPS:
        raise _bad("missing or unknown op")
    operation_id = header.get("operationId")
    if not isinstance(operation_id, str) or not operation_id:
        raise _bad("missing operationId")
    return Frame(header=header, body=data[end:])


def _non_negative_int(header: Mapping[str, Any], name: str) -> int:
    value = header.get(name)
    if type(value) is not int or value < 0:
        raise _bad(f"{name} must be a non-negative integer")
    return value


def _digest(header: Mapping[str, Any], name: str) -> str:
    value = header.get(name)
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise _bad(f"{name} must be 64 lowercase hex digits")
    return value


def encode_chunk_request(request: ChunkRequest) -> bytes:
    header = {
        "v": TELEPORT_VERSION,
        "op": "chunk",
        "operationId": request.operation_id,
        "offset": request.offset,
        "total": request.total,
        "size": request.size,
        "streamSha256": request.stream_sha256,
        "chunkSha256": request.chunk_sha256,
    }
    return encode_frame(header, request.data)


def decode_chunk_request(data: bytes) -> ChunkRequest:
    frame = decode_frame(data)
    header = frame.header
    if header["op"] != "chunk":
        raise _bad("not a chunk frame")
    size = _non_negative_int(header, "size")
    if size != len(frame.body):
        raise _bad("size does not match body length")
    return ChunkRequest(
        operation_id=header["operationId"],
        offset=_non_negative_int(header, "offset"),
        total=_non_negative_int(header, "total"),
        size=size,
        stream_sha256=_digest(header, "streamSha256"),
        chunk_sha256=_digest(header, "chunkSha256"),
        data=frame.body,
    )


def encode_reply(
    op: str, operation_id: str, fields: Mapping[str, Any] | None = None
) -> bytes:
    header = {"v": TELEPORT_VERSION, "op": op, "operationId": operation_id}
    header.update(fields or {})
    header["ok"] = True
    return encode_frame(header)


def encode_error_reply(op: str, operation_id: str, error: TeleportError) -> bytes:
    header = {
        "v": TELEPORT_VERSION,
        "op": op,
        "operationId": operation_id,
        "ok": False,
        "error": {"code": error.code, "message": error.message},
    }
    return encode_frame(header)


def decode_reply(data: bytes) -> Frame:
    frame = decode_frame(data)
    header = frame.header
    ok = header.get("ok")
    if type(ok) is not bool:
        raise _bad("reply ok must be a boolean")
    if not ok:
        error = header.get("error")
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not isinstance(error.get("message"), str)
        ):
            raise _bad("error reply needs error.code and error.message")
    return frame
