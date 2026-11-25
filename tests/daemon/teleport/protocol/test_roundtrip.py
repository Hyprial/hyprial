"""Slice 4a: prepare / receive / commit / abort encode-decode round trips.

Decisions are recorded in ``samples.py``; this module pins the round trips,
the exact wire header of each message, the optional-field defaults and the
``ok:false`` reply path.
"""

from __future__ import annotations

import pytest

from ..support import teleport
from .samples import (
    KINDS,
    MANIFEST_DIGEST,
    OPERATION_ID,
    PUBLIC_KEY,
    PUBLIC_KEY_B64,
    SOURCE_URI,
    STREAM_SHA256,
    TARGET_URI,
    base_headers,
    codec,
    protocol,
    sample_messages,
)


@pytest.mark.parametrize("kind", KINDS)
def test_round_trip_is_identity(kind: str) -> None:
    encode, decode = codec(kind)
    message = sample_messages()[kind]
    encoded = encode(message)
    assert isinstance(encoded, bytes)
    assert decode(encoded) == message


@pytest.mark.parametrize("kind", KINDS)
def test_encoded_frame_has_empty_body_and_matching_op(kind: str) -> None:
    encode, _ = codec(kind)
    frame = teleport().decode_frame(encode(sample_messages()[kind]))
    assert frame.body == b""
    assert frame.header["op"] == kind.split("_")[0]
    assert frame.header["operationId"] == OPERATION_ID
    assert frame.header["v"] == 1


@pytest.mark.parametrize("kind", KINDS)
def test_base_header_decodes(kind: str) -> None:
    _, decode = codec(kind)
    header = base_headers()[kind]
    decoded = decode(teleport().encode_frame(header))
    assert decoded.operation_id == OPERATION_ID


def _header(kind: str) -> dict:
    encode, _ = codec(kind)
    return teleport().decode_frame(encode(sample_messages()[kind])).header


def test_prepare_request_wire_header() -> None:
    assert _header("prepare_request") == {
        "v": 1,
        "op": "prepare",
        "operationId": OPERATION_ID,
        "sourceNode": "node-a",
        "targetNode": "node-b",
        "owner": "owner-1",
        "agent": "worker",
        "sourceAgentUri": SOURCE_URI,
        "harness": "claude",
        "routines": ["nightly", "weekly"],
        "dryRun": True,
    }


def test_prepare_reply_wire_header_carries_base64_key() -> None:
    assert _header("prepare_reply") == {
        "v": 1,
        "op": "prepare",
        "operationId": OPERATION_ID,
        "ok": True,
        "targetNode": "node-b",
        "recipientPublicKey": PUBLIC_KEY_B64,
        "dryRun": True,
    }


def test_receive_request_wire_header() -> None:
    assert _header("receive_request") == {
        "v": 1,
        "op": "receive",
        "operationId": OPERATION_ID,
        "manifestDigest": MANIFEST_DIGEST,
        "streamSha256": STREAM_SHA256,
        "harness": "codex",
        "args": ["--model", "m-1"],
        "sessionRef": "sess-1",
        "strictTimeoutSeconds": 12.5,
        "routines": [
            {"name": "nightly", "yaml": "name: nightly\n", "enabled": True},
            {"name": "weekly", "yaml": "name: weekly\n", "enabled": False},
        ],
    }


def test_receive_reply_wire_header() -> None:
    assert _header("receive_reply") == {
        "v": 1,
        "op": "receive",
        "operationId": OPERATION_ID,
        "ok": True,
        "agentUri": TARGET_URI,
        "sessionRef": "sess-1",
        "routines": [
            {"name": "nightly", "restoreEnabled": True},
            {"name": "weekly", "restoreEnabled": False},
        ],
    }


def test_commit_wire_headers() -> None:
    assert _header("commit_request") == {
        "v": 1,
        "op": "commit",
        "operationId": OPERATION_ID,
    }
    assert _header("commit_reply") == {
        "v": 1,
        "op": "commit",
        "operationId": OPERATION_ID,
        "ok": True,
        "routines": [
            {"name": "nightly", "enabled": True, "resumed": True},
            {"name": "weekly", "enabled": False, "resumed": False},
        ],
    }


def test_abort_wire_headers() -> None:
    assert _header("abort_request") == {
        "v": 1,
        "op": "abort",
        "operationId": OPERATION_ID,
        "reason": "chunk send failed",
    }
    assert _header("abort_reply") == {
        "v": 1,
        "op": "abort",
        "operationId": OPERATION_ID,
        "ok": True,
    }


def test_prepare_reply_decodes_raw_key_bytes() -> None:
    _, decode = codec("prepare_reply")
    reply = decode(teleport().encode_frame(base_headers()["prepare_reply"]))
    assert reply.recipient_public_key == PUBLIC_KEY
    assert reply.target_node == "node-b"
    assert reply.dry_run is False


def test_prepare_request_dry_run_defaults_to_false() -> None:
    header = base_headers()["prepare_request"]
    del header["dryRun"]
    _, decode = codec("prepare_request")
    request = decode(teleport().encode_frame(header))
    assert request.dry_run is False
    assert request.routines == ("nightly", "weekly")
    assert request.source_agent_uri == SOURCE_URI


def test_prepare_request_accepts_empty_routines() -> None:
    header = base_headers()["prepare_request"]
    header["routines"] = []
    _, decode = codec("prepare_request")
    assert decode(teleport().encode_frame(header)).routines == ()


def test_receive_request_optional_fields_default() -> None:
    header = base_headers()["receive_request"]
    del header["args"]
    del header["strictTimeoutSeconds"]
    _, decode = codec("receive_request")
    request = decode(teleport().encode_frame(header))
    assert request.args == ()
    assert request.strict_timeout_seconds == 60
    assert request.manifest_digest == MANIFEST_DIGEST
    assert request.stream_sha256 == STREAM_SHA256


def test_receive_request_session_ref_null_round_trips() -> None:
    p = protocol()
    message = p.ReceiveRequest(
        operation_id=OPERATION_ID,
        manifest_digest=MANIFEST_DIGEST,
        stream_sha256=STREAM_SHA256,
        harness="pi",
        session_ref=None,
        routines=(),
    )
    assert message.args == ()
    assert message.strict_timeout_seconds == 60
    encoded = p.encode_receive_request(message)
    header = teleport().decode_frame(encoded).header
    assert header["sessionRef"] is None
    assert header["routines"] == []
    assert header["args"] == []
    assert header["strictTimeoutSeconds"] == 60
    assert p.decode_receive_request(encoded) == message


def test_receive_reply_session_ref_null_round_trips() -> None:
    p = protocol()
    message = p.ReceiveReply(
        operation_id=OPERATION_ID, agent_uri=TARGET_URI, session_ref=None, routines=()
    )
    encoded = p.encode_receive_reply(message)
    assert teleport().decode_frame(encoded).header["sessionRef"] is None
    assert p.decode_receive_reply(encoded) == message


def test_abort_request_without_reason_omits_the_field() -> None:
    p = protocol()
    message = p.AbortRequest(operation_id=OPERATION_ID)
    assert message.reason is None
    encoded = p.encode_abort_request(message)
    assert "reason" not in teleport().decode_frame(encoded).header
    assert p.decode_abort_request(encoded) == message


def test_commit_reply_empty_routines_round_trips() -> None:
    p = protocol()
    message = p.CommitReply(operation_id=OPERATION_ID, routines=())
    assert p.decode_commit_reply(p.encode_commit_reply(message)) == message


def test_prepare_request_dry_run_default_on_dataclass() -> None:
    p = protocol()
    message = p.PrepareRequest(
        operation_id=OPERATION_ID,
        source_node="node-a",
        target_node="node-b",
        owner="owner-1",
        agent="worker",
        source_agent_uri=SOURCE_URI,
        harness="claude",
        routines=(),
    )
    assert message.dry_run is False
    header = teleport().decode_frame(p.encode_prepare_request(message)).header
    assert header["dryRun"] is False
    assert header["routines"] == []


def test_messages_are_frozen() -> None:
    message = sample_messages()["commit_request"]
    with pytest.raises(AttributeError):
        message.operation_id = "other"


@pytest.mark.parametrize("op", ("prepare", "receive", "commit", "abort"))
@pytest.mark.parametrize(
    "code", ("TELEPORT_AGENT_EXISTS", "TELEPORT_LANDING_FAILED", "TELEPORT_UNKNOWN_OPERATION")
)
def test_error_reply_raises_remote_code(op: str, code: str) -> None:
    module = teleport()
    _, decode = codec(f"{op}_reply")
    encoded = module.encode_error_reply(
        op, OPERATION_ID, module.TeleportError(code, "remote said no")
    )
    with pytest.raises(module.TeleportError) as caught:
        decode(encoded)
    assert caught.value.code == code
    assert caught.value.message == "remote said no"
