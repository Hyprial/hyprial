"""S3 controller: default-off listener, closed frame stages, limits, audit."""

from __future__ import annotations

import json
import socket

import pytest

from hyprial.shell.impl.gui.adapter.mobile.pairing import protocol as pairing_protocol
from hyprial.shell.impl.gui.adapter.mobile.pairing.server import (
    connect_pinned,
    read_frame,
)
from tests.shell.gui.adapter.mobile.helpers import NODE, OPERATOR, OWNER, PERMISSIONS
from tests.shell.gui.adapter.mobile.pairing.server.support import (
    AuditRecorder,
    RecordingSocket,
    build_server,
    new_client,
    open_channel,
    pairing_request_count,
    redeem_once,
    send_frame,
)


def _assert_eof(sock, *, timeout: float = 3.0) -> None:
    sock.settimeout(timeout)
    try:
        data = sock.recv(1)
    except OSError:
        data = b""
    assert data == b""


def _assert_error(recording: RecordingSocket, code: str) -> dict:
    frame = read_frame(recording, timeout=3.0)
    assert frame["type"] == "pairing.error"
    assert frame["code"] == code
    return frame


def _redeem_frame(client, code: str | None) -> dict:
    return client.redeem_start(
        code=code,
        device_id="dev-a",
        device_uid="uid-a",
        name="mate phone",
        owner=OWNER,
        permissions=PERMISSIONS,
    )


def test_server_is_off_until_started_and_close_releases_port(
    store, tls_material
):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = build_server(
        store,
        tls_material,
        port=port,
        handshake_timeout=1.0,
        idle_timeout=1.0,
    )
    assert server.started is False
    with pytest.raises(RuntimeError):
        _ = server.bound_port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1.0).close()

    server.start()
    try:
        assert server.started is True
        assert server.bound_port == port
        sock = connect_pinned(
            "127.0.0.1",
            port,
            fingerprint=tls_material.fingerprint,
            trust_certfile=tls_material.certfile,
            timeout=2.0,
        )
        sock.close()
    finally:
        server.close()

    assert server.started is False
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1.0).close()
    server.close()


def test_full_pairing_round_trip_and_host_never_sends_code(
    server_factory, store, tls_material, audit_recorder: AuditRecorder
):
    server = server_factory(authority=store)
    code, _expires = store.issue_pairing_code(operator=OPERATOR)

    recording, client, client_public, challenge = open_channel(
        server, tls_material
    )
    received = bytearray()
    try:
        assert challenge["type"] == "pairing.challenge"
        redeem = _redeem_frame(client, code)
        send_frame(recording, redeem)
        pending = client.accept_status(read_frame(recording, timeout=3.0))
        assert pending["status"] == "pending"
        assert "code" not in pending
        request_id = pending["requestId"]
        fingerprint = pending["fingerprint"]
        received.extend(recording.received)
    finally:
        recording.close()

    assert pairing_request_count(store) == 1
    store.approve_pairing_request(
        request_id,
        operator=OPERATOR,
        node_id=NODE,
        device_id="dev-a",
        client_public=client_public,
        fingerprint=fingerprint,
    )

    sock = connect_pinned(
        "127.0.0.1",
        server.bound_port,
        fingerprint=tls_material.fingerprint,
        trust_certfile=tls_material.certfile,
        timeout=3.0,
    )
    recording = RecordingSocket(sock)
    try:
        send_frame(recording, client.hello())
        client.accept_challenge(read_frame(recording, timeout=3.0))
        send_frame(recording, client.status_query(request_id))
        approved = client.accept_status(read_frame(recording, timeout=3.0))
        received.extend(recording.received)
    finally:
        recording.close()
        client.clear()

    assert approved["status"] == "approved"
    assert approved["device"]["deviceId"] == "dev-a"
    assert approved["device"]["targetNodeId"] == NODE
    assert approved["device"]["clientPublic"] == client_public

    wire = bytes(received)
    assert code.encode("utf-8") not in wire
    assert b'"code"' not in wire
    assert b"pairingAttemptId" not in wire
    assert b"clientProof" not in wire

    audit_recorder.wait_for("pairing-channel-closed")
    events = audit_recorder.snapshot()
    assert sum(
        1 for event in events if event["event"] == "pairing-channel-opened"
    ) == 2
    assert any(
        event["event"] == "pairing-channel-closed"
        and event.get("requestId") == request_id
        for event in events
    )
    assert all(
        event["nodeId"] == NODE and type(event["at"]) is int
        for event in events
    )


def test_redeem_before_challenge_is_rejected_without_consuming_code(
    server_factory, store, tls_material
):
    server = server_factory(authority=store)
    code, _expires = store.issue_pairing_code(operator=OPERATOR)
    client, client_public = new_client(tls_material)

    frame = pairing_protocol.build_redeem(
        mode="start",
        channel_binding="sha256:" + "00" * 32,
        pairing_attempt_id=b"\x01" * 32,
        code=code,
        device_id="dev-a",
        device_uid="uid-a",
        name="mate phone",
        owner=OWNER,
        client_public=client_public,
        permissions=PERMISSIONS,
        client_proof=b"\x00" * 32,
    )
    sock = connect_pinned(
        "127.0.0.1",
        server.bound_port,
        fingerprint=tls_material.fingerprint,
        trust_certfile=tls_material.certfile,
        timeout=3.0,
    )
    recording = RecordingSocket(sock)
    try:
        send_frame(recording, frame)
        _assert_error(recording, "pairing-frame-invalid")
    finally:
        recording.close()
        client.clear()

    assert pairing_request_count(store) == 0
    assert redeem_once(server, tls_material, code)["status"] == "pending"


@pytest.mark.parametrize(
    "frame_type",
    [
        "pairing.approve",
        "pairing.reject",
        "pairing.device",
        "pairing.grant",
        "pairing.unknown",
    ],
)
def test_unknown_and_approval_frames_are_rejected(
    server_factory, store, tls_material, frame_type: str
):
    server = server_factory(authority=store)
    recording, client, _public, _challenge = open_channel(
        server, tls_material
    )
    try:
        recording.sendall(
            json.dumps({"v": 1, "type": frame_type}).encode() + b"\n"
        )
        _assert_error(recording, "pairing-frame-invalid")
    finally:
        recording.close()
        client.clear()
    assert pairing_request_count(store) == 0


@pytest.mark.parametrize("kind", ["oversized", "duplicate", "unknown-field"])
def test_malformed_frames_are_rejected(
    server_factory, store, tls_material, kind: str
):
    server = server_factory(authority=store)
    if kind == "oversized":
        payload = (
            b'{"v":1,"type":"pairing.hello","x":"'
            + b"a" * 9000
            + b'"}\n'
        )
    elif kind == "duplicate":
        payload = b'{"v":1,"v":1,"type":"pairing.hello"}\n'
    else:
        client, _public = new_client(tls_material)
        hello = dict(client.hello())
        hello["extra"] = 1
        payload = json.dumps(hello).encode() + b"\n"

    sock = connect_pinned(
        "127.0.0.1",
        server.bound_port,
        fingerprint=tls_material.fingerprint,
        trust_certfile=tls_material.certfile,
        timeout=3.0,
    )
    recording = RecordingSocket(sock)
    try:
        try:
            recording.sendall(payload)
        except OSError:
            pass
        _assert_error(recording, "pairing-frame-invalid")
    finally:
        recording.close()
    assert pairing_request_count(store) == 0


def test_idle_timeout_closes_channel_and_audits(
    server_factory, store, tls_material, audit_recorder: AuditRecorder
):
    server = server_factory(authority=store, idle_timeout=0.2)
    sock = connect_pinned(
        "127.0.0.1",
        server.bound_port,
        fingerprint=tls_material.fingerprint,
        trust_certfile=tls_material.certfile,
        timeout=3.0,
    )
    recording = RecordingSocket(sock)
    try:
        _assert_eof(recording)
    finally:
        recording.close()

    closed = audit_recorder.wait_for("pairing-channel-closed")[-1]
    assert closed["errorCode"] == "pairing-idle-timeout"
    assert pairing_request_count(store) == 0


def test_handshake_timeout_closes_socket_and_audits(
    server_factory, store, audit_recorder: AuditRecorder, raw_socket
):
    server = server_factory(authority=store, handshake_timeout=0.2)
    raw = raw_socket(server)
    _assert_eof(raw)

    failed = audit_recorder.wait_for("pairing-tls-failed")[-1]
    assert failed["errorCode"] == "pairing-tls-handshake-failed"
    assert pairing_request_count(store) == 0


def test_connection_limit_refuses_excess_connections(
    server_factory, store, tls_material, audit_recorder: AuditRecorder,
    raw_socket,
):
    server = server_factory(
        authority=store, max_connections=1, idle_timeout=5.0
    )
    recording, client, _public, _challenge = open_channel(
        server, tls_material
    )
    try:
        raw = raw_socket(server)
        _assert_eof(raw)
        limited = audit_recorder.wait_for("pairing-connection-limited")[-1]
        assert limited["errorCode"] == "pairing-connection-limit"
    finally:
        recording.close()
        client.clear()

    audit_recorder.wait_for("pairing-channel-closed")
    second, second_client, _public, _challenge = open_channel(
        server, tls_material
    )
    second.close()
    second_client.clear()


def test_frame_rate_limit_rejects_operation_and_audits(
    server_factory, store, tls_material, audit_recorder: AuditRecorder
):
    server = server_factory(
        authority=store, frame_rate_limit=1, frame_rate_window=60.0
    )
    code, _expires = store.issue_pairing_code(operator=OPERATOR)
    recording, client, client_public, _challenge = open_channel(
        server, tls_material
    )
    try:
        send_frame(recording, _redeem_frame(client, code))
        error = _assert_error(recording, "pairing-rate-limited")
        assert 1 <= error["retryAfterSeconds"] <= 3600
    finally:
        recording.close()
        client.clear()

    limited = audit_recorder.wait_for("pairing-frame-rate-limited")[-1]
    assert limited["errorCode"] == "pairing-rate-limited"
    assert pairing_request_count(store) == 0

    replacement = server_factory(authority=store)
    assert redeem_once(replacement, tls_material, code)["status"] == "pending"


def test_channel_closes_after_one_operation(
    server_factory, store, tls_material
):
    server = server_factory(authority=store)
    code, _expires = store.issue_pairing_code(operator=OPERATOR)
    recording, client, client_public, _challenge = open_channel(
        server, tls_material
    )
    try:
        send_frame(recording, _redeem_frame(client, code))
        pending = client.accept_status(read_frame(recording, timeout=3.0))
        assert pending["status"] == "pending"
        _assert_eof(recording)
    finally:
        recording.close()
        client.clear()


def test_audit_entries_have_no_pairing_secrets(
    server_factory, store, tls_material, audit_recorder: AuditRecorder
):
    server = server_factory(authority=store)
    code, _expires = store.issue_pairing_code(operator=OPERATOR)
    recording, client, client_public, _challenge = open_channel(
        server, tls_material
    )
    try:
        redeem = _redeem_frame(client, code)
        send_frame(recording, redeem)
        pending = client.accept_status(read_frame(recording, timeout=3.0))
        assert pending["status"] == "pending"
    finally:
        recording.close()
        client.clear()

    audit_recorder.wait_for("pairing-channel-closed")
    events = audit_recorder.snapshot()
    text = json.dumps(events, sort_keys=True)
    for secret in (
        code,
        redeem["pairingAttemptId"],
        redeem["clientProof"],
    ):
        assert secret not in text
    for forbidden in (
        "clientProof",
        "pairingAttemptId",
        "requestKey",
        "PRIVATE KEY",
        "signature",
    ):
        assert forbidden not in text

    opened = [
        event for event in events if event["event"] == "pairing-channel-opened"
    ]
    closed = [
        event for event in events if event["event"] == "pairing-channel-closed"
    ]
    assert opened and closed
    assert closed[-1]["requestId"] == pending["requestId"]
    assert "errorCode" not in closed[-1]
    assert all(
        event["nodeId"] == NODE and type(event["at"]) is int
        for event in events
    )
