"""The query-handler binding: all five ops, reply keys and malformed frames."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.transport import KeySpace

from ..support import teleport
from .support import (
    NODE,
    PUBLIC_KEY,
    TARGET_URI,
    RecordingSession,
    chunks,
    key,
    make_service,
    packed_bundle,
    prepare_request,
    protocol,
    receive_request,
    target,
)


def _single(replies, expected_key: str) -> bytes:
    replies = list(replies)
    assert len(replies) == 1
    reply_key, payload = replies[0]
    assert reply_key == expected_key
    return payload


def _error(payload: bytes) -> dict:
    header = teleport().decode_reply(payload).header
    assert header["ok"] is False
    return header


def test_all_five_ops_are_served(tmp_path: Path) -> None:
    p = protocol()
    service, host, receiver = make_service(tmp_path)
    packed = packed_bundle(tmp_path)

    k = key("prepare", "op-1")
    prepared = p.decode_prepare_reply(
        _single(service.handle_query(k + "?x=1", p.encode_prepare_request(prepare_request("op-1"))), k)
    )
    assert prepared.target_node == NODE
    assert prepared.recipient_public_key == PUBLIC_KEY

    k = key("chunk", "op-1")
    for request in chunks("op-1", packed):
        header = teleport().decode_reply(
            _single(service.handle_query(k, teleport().encode_chunk_request(request)), k)
        ).header
        assert header["ok"] is True
        assert header["persisted"] == request.offset + request.size

    k = key("receive", "op-1")
    received = p.decode_receive_reply(
        _single(service.handle_query(k, p.encode_receive_request(receive_request("op-1", packed))), k)
    )
    assert received.agent_uri == TARGET_URI

    k = key("commit", "op-1")
    committed = p.decode_commit_reply(
        _single(service.handle_query(k, p.encode_commit_request(p.CommitRequest(operation_id="op-1"))), k)
    )
    assert committed.routines == ()
    assert service.state("op-1") == "committed"

    k = key("abort", "op-9")
    aborted = p.decode_abort_reply(
        _single(service.handle_query(k, p.encode_abort_request(p.AbortRequest(operation_id="op-9"))), k)
    )
    assert aborted.operation_id == "op-9"
    assert host.calls == ["land", "activate", "start"]
    service.close()


def test_business_failure_is_an_error_reply(tmp_path: Path) -> None:
    p = protocol()
    service, _host, _receiver = make_service(tmp_path)
    k = key("commit", "op-x")
    header = _error(
        _single(service.handle_query(k, p.encode_commit_request(p.CommitRequest(operation_id="op-x"))), k)
    )
    assert header["op"] == "commit"
    assert header["operationId"] == "op-x"
    assert header["error"]["code"] == "TELEPORT_UNKNOWN_OPERATION"
    service.close()


def _prepare_header(**changes) -> dict:
    header = {
        "v": 1,
        "op": "prepare",
        "operationId": "op-1",
        "sourceNode": "node-a",
        "targetNode": NODE,
        "owner": "owner-1",
        "agent": "worker",
        "sourceAgentUri": "agent://owner-1/node-a/worker",
        "harness": "claude",
        "routines": [],
    }
    header.update(changes)
    return {name: value for name, value in header.items() if value is not None}


MALFORMED = [
    pytest.param("prepare", None, id="no-payload"),
    pytest.param("prepare", b"", id="empty-payload"),
    pytest.param("prepare", b"\x00\x00\x00\x09{}", id="truncated"),
    pytest.param("prepare", b"\x00\x00\x00\x02[]", id="header-not-object"),
    pytest.param("prepare", "missing-agent", id="missing-field"),
    pytest.param("prepare", "routines-not-array", id="wrong-type"),
    pytest.param("prepare", "op-mismatch", id="header-op-differs-from-key"),
    pytest.param("prepare", "id-mismatch", id="header-id-differs-from-key"),
    pytest.param("receive", "prepare-frame", id="prepare-frame-on-receive-key"),
    pytest.param("commit", "with-body", id="commit-with-body"),
    pytest.param("chunk", "bad-chunk", id="chunk-size-mismatch"),
]


def _malformed_payload(kind):
    encode = teleport().encode_frame
    if kind is None or isinstance(kind, bytes):
        return kind
    if kind == "missing-agent":
        return encode(_prepare_header(agent=None))
    if kind == "routines-not-array":
        return encode(_prepare_header(routines="nightly"))
    if kind == "op-mismatch":
        return encode({"v": 1, "op": "abort", "operationId": "op-1"})
    if kind == "id-mismatch":
        return encode(_prepare_header(operationId="op-2"))
    if kind == "prepare-frame":
        return encode(_prepare_header())
    if kind == "with-body":
        return encode({"v": 1, "op": "commit", "operationId": "op-1"}, b"x")
    if kind == "bad-chunk":
        header = {
            "v": 1,
            "op": "chunk",
            "operationId": "op-1",
            "offset": 0,
            "total": 4,
            "size": 4,
            "streamSha256": "0" * 64,
            "chunkSha256": "0" * 64,
        }
        return encode(header, b"xy")
    raise AssertionError(kind)


@pytest.mark.parametrize(("op", "kind"), MALFORMED)
def test_malformed_frames_are_bad_frame_keyed_by_the_concrete_key(
    tmp_path: Path, op: str, kind
) -> None:
    service, host, receiver = make_service(tmp_path)
    if op != "prepare":
        service.prepare(prepare_request("op-1"))
    k = key(op, "op-1")
    header = _error(_single(service.handle_query(k + "?z=1", _malformed_payload(kind)), k))
    assert header["op"] == op
    assert header["operationId"] == "op-1"
    assert header["error"]["code"] == "TELEPORT_BAD_FRAME"
    assert host.calls == []
    if op == "prepare":
        assert service.state("op-1") is None
    service.close()


def test_key_for_another_node_is_wrong_target(tmp_path: Path) -> None:
    service, _host, _receiver = make_service(tmp_path)
    k = key("prepare", "op-1", node="node-elsewhere")
    payload = protocol().encode_prepare_request(prepare_request("op-1", target_node="node-elsewhere"))
    header = _error(_single(service.handle_query(k, payload), k))
    assert header["error"]["code"] == "TELEPORT_WRONG_TARGET"
    assert service.state("op-1") is None
    service.close()


def test_header_target_node_must_equal_the_key_node(tmp_path: Path) -> None:
    service, _host, _receiver = make_service(tmp_path)
    k = key("prepare", "op-1")
    payload = protocol().encode_prepare_request(prepare_request("op-1", target_node="node-elsewhere"))
    header = _error(_single(service.handle_query(k, payload), k))
    assert header["error"]["code"] == "TELEPORT_WRONG_TARGET"
    service.close()


def test_non_teleport_key_gets_no_reply(tmp_path: Path) -> None:
    service, _host, _receiver = make_service(tmp_path)
    payload = protocol().encode_prepare_request(prepare_request("op-1"))
    assert service.handle_query("hyprial/v1/something/else", payload) == []
    assert service.state("op-1") is None
    service.close()


def test_bind_declares_one_handler_for_the_local_node(tmp_path: Path) -> None:
    service, _host, _receiver = make_service(tmp_path)
    session = RecordingSession()
    registration = target().bind_target_service(session, service)
    assert [declared for declared, _handler in session.declared] == [
        KeySpace().agent_teleport_all(NODE)
    ]
    k = key("prepare", "op-1")
    payload = protocol().encode_prepare_request(prepare_request("op-1"))
    reply = protocol().decode_prepare_reply(_single(session.handler()(k, payload), k))
    assert reply.target_node == NODE
    registration.close()
    assert session.registrations[0].closed is True
    service.close()
