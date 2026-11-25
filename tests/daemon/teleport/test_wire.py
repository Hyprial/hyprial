"""Slice 2 wire: key layout, frame codec and the ``TELEPORT_*`` error type.

Decisions pinned here:

- Key: ``hyprial/v1/agent-teleport/<targetNode>/<op>/<operationId>``; node and
  operationId segments use ``KeySpace.encode_identity`` so any character that
  Zenoh treats specially is escaped.  ``KeySpace.agent_teleport_all(node)`` is
  the declaration-side wildcard ``.../agent-teleport/<node>/**``.
- ``parse_teleport_key`` accepts a query selector (drops a ``?...`` suffix) and
  raises ``ValueError`` for anything that is not a teleport key.
- Frame: 4-byte big-endian header length, canonical UTF-8 JSON header
  (``sort_keys``, compact separators, ``ensure_ascii=False``), raw body.
- Every malformed frame raises ``TeleportError`` whose ``code`` is
  ``TELEPORT_BAD_FRAME``.
"""

from __future__ import annotations

import json
import struct

import pytest

from hyprial.daemon.impl.transport import KeySpace

from .support import FROZEN_ERROR_CODES, sha256_hex, teleport

OPS = ("prepare", "chunk", "receive", "commit", "abort")


def _raw_frame(header: object, body: bytes = b"") -> bytes:
    encoded = json.dumps(header).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded + body


def _bad_frame(callable_, *args) -> None:
    module = teleport()
    with pytest.raises(module.TeleportError) as caught:
        callable_(*args)
    assert caught.value.code == "TELEPORT_BAD_FRAME"


def test_constants_and_frozen_error_codes() -> None:
    module = teleport()
    assert module.TELEPORT_CHUNK_BYTES == 768_000
    assert tuple(module.TELEPORT_OPS) == OPS
    assert frozenset(module.TELEPORT_ERROR_CODES) == FROZEN_ERROR_CODES


def test_teleport_error_carries_code_and_message() -> None:
    module = teleport()
    error = module.TeleportError("TELEPORT_OFFSET_GAP", "offset 9 beyond 0")
    assert isinstance(error, Exception)
    assert error.code == "TELEPORT_OFFSET_GAP"
    assert error.message == "offset 9 beyond 0"


def test_keyspace_builds_plain_teleport_key() -> None:
    keys = KeySpace()
    assert (
        keys.agent_teleport("node-a", "chunk", "op-1")
        == "hyprial/v1/agent-teleport/node-a/chunk/op-1"
    )
    assert keys.agent_teleport_all("node-a") == "hyprial/v1/agent-teleport/node-a/**"


@pytest.mark.parametrize("op", OPS)
def test_key_round_trips_for_every_op(op: str) -> None:
    module = teleport()
    key = KeySpace().agent_teleport("node-a", op, "op-1")
    parsed = module.parse_teleport_key(key)
    assert parsed == module.TeleportKey(target_node="node-a", op=op, operation_id="op-1")


@pytest.mark.parametrize(
    ("node", "operation_id"),
    [
        ("user@host/with space", "op/with/slashes"),
        ("node*star", "op?query=1"),
        ("node$dollar#hash", "op**"),
        ("~looks-encoded", "~also"),
        ("节点", "操作-1"),
    ],
)
def test_key_escapes_unsafe_segments_and_round_trips(node: str, operation_id: str) -> None:
    module = teleport()
    key = KeySpace().agent_teleport(node, "abort", operation_id)
    assert key.startswith("hyprial/v1/agent-teleport/")
    assert len(key.split("/")) == 6
    assert not any(ch in key for ch in "*?$# ")
    parsed = module.parse_teleport_key(key)
    assert parsed.target_node == node
    assert parsed.op == "abort"
    assert parsed.operation_id == operation_id
    wildcard = KeySpace().agent_teleport_all(node)
    assert wildcard.split("/")[3] == key.split("/")[3]


def test_parse_drops_selector_parameters() -> None:
    module = teleport()
    key = KeySpace().agent_teleport("node-a", "chunk", "op-1")
    parsed = module.parse_teleport_key(key + "?_anyke=1")
    assert parsed.operation_id == "op-1"


@pytest.mark.parametrize(
    ("node", "op", "operation_id"),
    [("", "chunk", "op"), ("node", "chunk", ""), ("node", "push", "op")],
)
def test_keyspace_rejects_invalid_parts(node: str, op: str, operation_id: str) -> None:
    with pytest.raises(ValueError):
        KeySpace().agent_teleport(node, op, operation_id)


@pytest.mark.parametrize(
    "key",
    [
        "hyprial/v1/inbox/node-a/chunk/op-1",
        "hyprial/v2/agent-teleport/node-a/chunk/op-1",
        "hyprial/v1/agent-teleport/node-a/chunk",
        "hyprial/v1/agent-teleport/node-a/chunk/op-1/extra",
        "hyprial/v1/agent-teleport/node-a/push/op-1",
        "hyprial/v1/agent-teleport/node-a/**",
    ],
)
def test_parse_rejects_foreign_keys(key: str) -> None:
    module = teleport()
    with pytest.raises(ValueError):
        module.parse_teleport_key(key)


def test_frame_round_trip_with_raw_body() -> None:
    module = teleport()
    header = {"v": 1, "op": "chunk", "operationId": "op-1", "extra": "é"}
    body = bytes(range(256)) * 3
    encoded = module.encode_frame(header, body)
    frame = module.decode_frame(encoded)
    assert frame.header == header
    assert frame.body == body


def test_frame_layout_is_length_prefixed_canonical_json() -> None:
    module = teleport()
    header = {"operationId": "op-1", "op": "abort", "v": 1, "note": "é"}
    encoded = module.encode_frame(header)
    canonical = json.dumps(
        header, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert encoded[:4] == struct.pack(">I", len(canonical))
    assert encoded[4:] == canonical
    reordered = dict(reversed(list(header.items())))
    assert module.encode_frame(reordered) == encoded
    assert module.decode_frame(encoded).body == b""


def test_body_is_never_base64() -> None:
    module = teleport()
    body = b"\x00\xffraw"
    encoded = module.encode_frame({"v": 1, "op": "chunk", "operationId": "o"}, body)
    assert encoded.endswith(body)


@pytest.mark.parametrize("data", [b"", b"\x00", b"\x00\x00\x00"])
def test_decode_rejects_truncated_length_prefix(data: bytes) -> None:
    _bad_frame(teleport().decode_frame, data)


def test_decode_rejects_header_length_beyond_buffer() -> None:
    module = teleport()
    good = module.encode_frame({"v": 1, "op": "abort", "operationId": "op-1"})
    _bad_frame(module.decode_frame, good[:-1])
    _bad_frame(module.decode_frame, struct.pack(">I", 0xFFFFFFFF) + b"{}")
    _bad_frame(module.decode_frame, struct.pack(">I", 0))


@pytest.mark.parametrize(
    "header_bytes",
    [b"not json", b"\xff\xfe\xfd", b"[1, 2]", b'"string"', b"null"],
)
def test_decode_rejects_non_object_json_header(header_bytes: bytes) -> None:
    data = struct.pack(">I", len(header_bytes)) + header_bytes
    _bad_frame(teleport().decode_frame, data)


@pytest.mark.parametrize("missing", ["v", "op", "operationId"])
def test_decode_rejects_missing_common_field(missing: str) -> None:
    header = {"v": 1, "op": "abort", "operationId": "op-1"}
    del header[missing]
    _bad_frame(teleport().decode_frame, _raw_frame(header))


@pytest.mark.parametrize("version", [0, 2, "1", True, 1.0, None])
def test_decode_rejects_version_other_than_one(version: object) -> None:
    header = {"v": version, "op": "abort", "operationId": "op-1"}
    _bad_frame(teleport().decode_frame, _raw_frame(header))


@pytest.mark.parametrize(
    "patch",
    [{"op": "push"}, {"op": 3}, {"operationId": ""}, {"operationId": 5}],
)
def test_decode_rejects_bad_op_or_operation_id(patch: dict) -> None:
    header = {"v": 1, "op": "abort", "operationId": "op-1"} | patch
    _bad_frame(teleport().decode_frame, _raw_frame(header))


def _chunk_header(**overrides: object) -> dict:
    body = b"abcdef"
    header = {
        "v": 1,
        "op": "chunk",
        "operationId": "op-1",
        "offset": 0,
        "total": 6,
        "size": 6,
        "streamSha256": sha256_hex(body),
        "chunkSha256": sha256_hex(body),
    }
    header.update(overrides)
    return header


def test_chunk_request_round_trip_and_exact_header() -> None:
    module = teleport()
    body = b"abcdef"
    request = module.ChunkRequest(
        operation_id="op-1",
        offset=0,
        total=6,
        size=6,
        stream_sha256=sha256_hex(body),
        chunk_sha256=sha256_hex(body),
        data=body,
    )
    encoded = module.encode_chunk_request(request)
    frame = module.decode_frame(encoded)
    assert frame.header == _chunk_header()
    assert frame.body == body
    assert module.decode_chunk_request(encoded) == request


@pytest.mark.parametrize(
    "field", ["offset", "total", "size", "streamSha256", "chunkSha256"]
)
def test_chunk_request_rejects_missing_field(field: str) -> None:
    header = _chunk_header()
    del header[field]
    _bad_frame(teleport().decode_chunk_request, _raw_frame(header, b"abcdef"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"op": "abort"},
        {"offset": -1},
        {"offset": True},
        {"offset": "0"},
        {"total": -6},
        {"size": 5},
        {"size": 7},
        {"chunkSha256": "nothex"},
        {"streamSha256": "A" * 64},
        {"streamSha256": 12},
    ],
)
def test_chunk_request_rejects_ill_typed_fields(overrides: dict) -> None:
    _bad_frame(
        teleport().decode_chunk_request,
        _raw_frame(_chunk_header(**overrides), b"abcdef"),
    )


def test_ok_reply_round_trip() -> None:
    module = teleport()
    encoded = module.encode_reply("chunk", "op-1", {"persisted": 42})
    frame = module.decode_reply(encoded)
    assert frame.header == {
        "v": 1,
        "op": "chunk",
        "operationId": "op-1",
        "ok": True,
        "persisted": 42,
    }
    assert frame.body == b""


def test_error_reply_round_trip() -> None:
    module = teleport()
    error = module.TeleportError("TELEPORT_OFFSET_GAP", "gap at 9")
    frame = module.decode_reply(module.encode_error_reply("chunk", "op-1", error))
    assert frame.header == {
        "v": 1,
        "op": "chunk",
        "operationId": "op-1",
        "ok": False,
        "error": {"code": "TELEPORT_OFFSET_GAP", "message": "gap at 9"},
    }


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"ok": "true"},
        {"ok": False},
        {"ok": False, "error": "TELEPORT_OFFSET_GAP"},
        {"ok": False, "error": {"message": "x"}},
        {"ok": False, "error": {"code": "TELEPORT_OFFSET_GAP"}},
    ],
)
def test_decode_reply_rejects_malformed_status(patch: dict) -> None:
    header = {"v": 1, "op": "chunk", "operationId": "op-1"} | patch
    _bad_frame(teleport().decode_reply, _raw_frame(header))
