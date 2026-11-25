"""Slice 4a: malformed prepare / receive / commit / abort frames are BAD_FRAME.

Each case starts from a valid header in ``samples.base_headers()`` and makes a
single change: drop a required field, replace a value with a wrong type, use
an unknown or mismatched ``op``, or attach a body.  Every one must raise
``TeleportError`` with code ``TELEPORT_BAD_FRAME``.
"""

from __future__ import annotations

import base64
import struct

import pytest

from ..support import teleport
from .samples import KINDS, base_headers, codec

_MISSING = object()

REQUIRED = {
    "prepare_request": (
        "v",
        "op",
        "operationId",
        "sourceNode",
        "targetNode",
        "owner",
        "agent",
        "sourceAgentUri",
        "harness",
        "routines",
    ),
    "prepare_reply": ("ok", "operationId", "targetNode", "recipientPublicKey", "dryRun"),
    "receive_request": (
        "operationId",
        "manifestDigest",
        "streamSha256",
        "harness",
        "sessionRef",
        "routines",
    ),
    "receive_reply": ("ok", "operationId", "agentUri", "sessionRef", "routines"),
    "commit_request": ("v", "op", "operationId"),
    "commit_reply": ("ok", "operationId", "routines"),
    "abort_request": ("op", "operationId"),
    "abort_reply": ("ok", "op", "operationId"),
}

MISSING_CASES = [
    pytest.param(kind, field, id=f"{kind}-missing-{field}")
    for kind, fields in REQUIRED.items()
    for field in fields
]

_SHORT_KEY = base64.b64encode(bytes(31)).decode("ascii")
_LONG_KEY = base64.b64encode(bytes(33)).decode("ascii")
_ROUTINE = {"name": "nightly", "yaml": "name: nightly\n", "enabled": True}

WRONG_TYPES = [
    ("prepare_request", "operationId", ""),
    ("prepare_request", "sourceNode", 5),
    ("prepare_request", "sourceNode", ""),
    ("prepare_request", "targetNode", None),
    ("prepare_request", "owner", ["owner-1"]),
    ("prepare_request", "agent", ""),
    ("prepare_request", "agent", 1),
    ("prepare_request", "sourceAgentUri", {}),
    ("prepare_request", "harness", True),
    ("prepare_request", "harness", ""),
    ("prepare_request", "routines", "nightly"),
    ("prepare_request", "routines", [1]),
    ("prepare_request", "routines", [""]),
    ("prepare_request", "routines", None),
    ("prepare_request", "dryRun", "yes"),
    ("prepare_request", "dryRun", 1),
    ("prepare_request", "dryRun", None),
    ("prepare_reply", "ok", "true"),
    ("prepare_reply", "targetNode", 7),
    ("prepare_reply", "targetNode", ""),
    ("prepare_reply", "recipientPublicKey", "not base64!!"),
    ("prepare_reply", "recipientPublicKey", _SHORT_KEY),
    ("prepare_reply", "recipientPublicKey", _LONG_KEY),
    ("prepare_reply", "recipientPublicKey", 123),
    ("prepare_reply", "recipientPublicKey", None),
    ("prepare_reply", "dryRun", "false"),
    ("prepare_reply", "dryRun", 0),
    ("receive_request", "manifestDigest", "A1" * 32),
    ("receive_request", "manifestDigest", "abc"),
    ("receive_request", "manifestDigest", 7),
    ("receive_request", "streamSha256", "g" * 64),
    ("receive_request", "streamSha256", "0f" * 33),
    ("receive_request", "streamSha256", None),
    ("receive_request", "harness", None),
    ("receive_request", "harness", ""),
    ("receive_request", "args", "--model"),
    ("receive_request", "args", [1]),
    ("receive_request", "args", None),
    ("receive_request", "sessionRef", 5),
    ("receive_request", "sessionRef", ""),
    ("receive_request", "sessionRef", ["sess-1"]),
    ("receive_request", "strictTimeoutSeconds", "60"),
    ("receive_request", "strictTimeoutSeconds", True),
    ("receive_request", "strictTimeoutSeconds", 0),
    ("receive_request", "strictTimeoutSeconds", -1),
    ("receive_request", "strictTimeoutSeconds", None),
    ("receive_request", "routines", {}),
    ("receive_request", "routines", None),
    ("receive_request", "routines", ["nightly"]),
    ("receive_request", "routines", [{"name": "nightly", "yaml": "y"}]),
    ("receive_request", "routines", [{**_ROUTINE, "name": 1}]),
    ("receive_request", "routines", [{**_ROUTINE, "name": ""}]),
    ("receive_request", "routines", [{**_ROUTINE, "yaml": 5}]),
    ("receive_request", "routines", [{**_ROUTINE, "enabled": "true"}]),
    ("receive_reply", "agentUri", None),
    ("receive_reply", "agentUri", ""),
    ("receive_reply", "sessionRef", 1),
    ("receive_reply", "sessionRef", ""),
    ("receive_reply", "routines", "nightly"),
    ("receive_reply", "routines", [{"name": "nightly"}]),
    ("receive_reply", "routines", [{"name": "nightly", "restoreEnabled": "yes"}]),
    ("receive_reply", "routines", [{"name": 3, "restoreEnabled": True}]),
    ("commit_reply", "routines", None),
    ("commit_reply", "routines", {}),
    ("commit_reply", "routines", [{"name": "nightly", "enabled": True}]),
    ("commit_reply", "routines", [{"name": "nightly", "enabled": True, "resumed": 1}]),
    ("commit_reply", "routines", [{"name": "nightly", "enabled": None, "resumed": True}]),
    ("commit_reply", "routines", [{"enabled": True, "resumed": True}]),
    ("abort_request", "reason", 5),
    ("abort_request", "reason", None),
    ("abort_request", "operationId", 9),
    ("abort_reply", "ok", 1),
]

WRONG_TYPE_CASES = [
    pytest.param(kind, field, value, id=f"{kind}-{field}-{index}")
    for index, (kind, field, value) in enumerate(WRONG_TYPES)
]


def _assert_bad_frame(kind: str, data: bytes) -> None:
    module = teleport()
    _, decode = codec(kind)
    with pytest.raises(module.TeleportError) as caught:
        decode(data)
    assert caught.value.code == "TELEPORT_BAD_FRAME"


def _frame(header: dict, body: bytes = b"") -> bytes:
    return teleport().encode_frame(header, body)


@pytest.mark.parametrize(("kind", "field"), MISSING_CASES)
def test_missing_required_field_is_bad_frame(kind: str, field: str) -> None:
    header = base_headers()[kind]
    del header[field]
    _assert_bad_frame(kind, _frame(header))


@pytest.mark.parametrize(("kind", "field", "value"), WRONG_TYPE_CASES)
def test_wrong_type_is_bad_frame(kind: str, field: str, value: object) -> None:
    header = base_headers()[kind]
    header[field] = value
    _assert_bad_frame(kind, _frame(header))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("op", ("bogus", "PREPARE", "", None, 3))
def test_unknown_op_is_bad_frame(kind: str, op: object) -> None:
    header = base_headers()[kind]
    header["op"] = op
    _assert_bad_frame(kind, _frame(header))


@pytest.mark.parametrize("kind", KINDS)
def test_mismatched_op_is_bad_frame(kind: str) -> None:
    own = kind.split("_")[0]
    other = "commit" if own != "commit" else "abort"
    header = base_headers()[kind]
    header["op"] = other
    _assert_bad_frame(kind, _frame(header))


@pytest.mark.parametrize("kind", KINDS)
def test_chunk_op_is_bad_frame(kind: str) -> None:
    header = base_headers()[kind]
    header["op"] = "chunk"
    _assert_bad_frame(kind, _frame(header))


@pytest.mark.parametrize("kind", KINDS)
def test_non_empty_body_is_bad_frame(kind: str) -> None:
    _assert_bad_frame(kind, _frame(base_headers()[kind], b"x"))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("version", (2, "1", 1.0, True))
def test_wrong_version_is_bad_frame(kind: str, version: object) -> None:
    header = base_headers()[kind]
    header["v"] = version
    _assert_bad_frame(kind, _frame(header))


@pytest.mark.parametrize("kind", KINDS)
def test_truncated_frame_is_bad_frame(kind: str) -> None:
    data = _frame(base_headers()[kind])
    _assert_bad_frame(kind, data[:-1])


@pytest.mark.parametrize("kind", KINDS)
def test_non_object_header_is_bad_frame(kind: str) -> None:
    encoded = b"[]"
    _assert_bad_frame(kind, struct.pack(">I", len(encoded)) + encoded)


@pytest.mark.parametrize("kind", [k for k in KINDS if k.endswith("_reply")])
def test_reply_with_malformed_error_is_bad_frame(kind: str) -> None:
    header = base_headers()[kind]
    header["ok"] = False
    header["error"] = {"code": 5}
    _assert_bad_frame(kind, _frame(header))
