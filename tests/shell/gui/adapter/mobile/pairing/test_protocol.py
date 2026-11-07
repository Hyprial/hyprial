"""Closed-set, fail-closed frame codec tests for the S1 pairing library.

These tests intentionally exercise the wire-facing parser rather than the
state machine: one UTF-8 JSON object per line, no unknown or duplicate keys,
no non-finite numbers, no bool-as-int, and no trailing bytes.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hyprial.shell.impl.gui.adapter.mobile.pairing import (
    MAX_FRAME_BYTES,
    PairingError,
    build_challenge,
    build_error,
    build_hello,
    build_redeem,
    build_status_query,
    build_status_response,
    encode_frame,
    parse_frame,
)

NODE = "node:dev000000001.local"
PIN = "sha256:" + "ab" * 32
FINGERPRINT = "ef" * 32
REQUEST_ID = "pair-" + "cd" * 16
CHANNEL_BINDING = "sha256:" + "12" * 32
OWNER = "agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"
CLIENT_PRIVATE_KEY = ec.derive_private_key(int("11" * 32, 16), ec.SECP256R1())
HOST_DERIVE_PRIVATE_KEY = ec.derive_private_key(int("33" * 32, 16), ec.SECP256R1())


def _raw(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


CLIENT_PUBLIC = _raw(CLIENT_PRIVATE_KEY.public_key())
HOST_DERIVE_PUBLIC = _raw(HOST_DERIVE_PRIVATE_KEY.public_key())
CLIENT_NONCE = bytes(range(32))
HOST_NONCE = bytes(range(32, 64))
PAIRING_ATTEMPT_ID = bytes(range(64, 96))


def _hello(**overrides: object) -> dict:
    frame = build_hello(
        node_id=NODE,
        host_key_fingerprint=PIN,
        client_nonce=CLIENT_NONCE,
        client_public=CLIENT_PUBLIC,
    )
    frame.update(overrides)
    return parse_frame(encode_frame(frame))


def _challenge(**overrides: object) -> dict:
    frame = build_challenge(
        node_id=NODE,
        host_key_fingerprint=PIN,
        client_nonce=CLIENT_NONCE,
        client_public=CLIENT_PUBLIC,
        host_nonce=HOST_NONCE,
        host_derive_public=HOST_DERIVE_PUBLIC,
        server_time=1_000_000,
        signature=b"\x01" * 64,
    )
    frame.update(overrides)
    return parse_frame(encode_frame(frame))


def _redeem(**overrides: object) -> dict:
    frame = build_redeem(
        mode="start",
        channel_binding=CHANNEL_BINDING,
        pairing_attempt_id=PAIRING_ATTEMPT_ID,
        code="ABCDEFGHJK",
        device_id="dev-a",
        device_uid="uid-a",
        name="mate phone",
        owner=OWNER,
        client_public=CLIENT_PUBLIC,
        permissions=["sessions.read"],
        client_proof=b"\x02" * 32,
    )
    aliases = {"device_id": "deviceId", "client_proof": "clientProof"}
    for key, value in overrides.items():
        frame[aliases.get(key, key)] = value
    return parse_frame(encode_frame(frame))


def _device(**overrides: object) -> dict:
    device = {
        "deviceId": "dev-a",
        "deviceGeneration": 1,
        "targetNodeId": NODE,
        "clientPublic": _b64(CLIENT_PUBLIC),
        "owner": OWNER,
        "permissions": ["sessions.read"],
        "approvedAt": 1_000_001,
        "status": "active",
        "revokedAt": None,
    }
    device.update(overrides)
    return device


def _status_response(**overrides: object) -> dict:
    status = overrides.get("status", "pending")
    kwargs: dict = {
        "request_id": REQUEST_ID,
        "node_id": NODE,
        "fingerprint": FINGERPRINT,
        "requested_at": 1_000_000,
        "expires_at": 1_000_300,
        "status": status,
    }
    if status == "pending":
        kwargs["poll_after_ms"] = 2_000
    kwargs.update(overrides)
    return build_status_response(**kwargs)


def _refusal_code(callable_) -> str:
    with pytest.raises(PairingError) as caught:
        callable_()
    return caught.value.code


def test_hello_roundtrip_is_one_canonical_line() -> None:
    frame = _hello()
    encoded = encode_frame(frame)
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert len(encoded) <= MAX_FRAME_BYTES
    parsed = parse_frame(encoded)
    assert parsed == frame
    assert parsed["protocol"] == "m11-pairing/1"


def test_unknown_and_missing_fields_are_frame_invalid() -> None:
    assert _refusal_code(lambda: encode_frame(_hello(extra="no"))) == (
        "pairing-frame-invalid"
    )
    missing = _hello()
    del missing["clientNonce"]
    assert _refusal_code(lambda: encode_frame(missing)) == "pairing-frame-invalid"


def test_wrong_protocol_is_protocol_unsupported() -> None:
    assert _refusal_code(
        lambda: encode_frame(_hello(protocol="m11-pairing/0"))
    ) == "pairing-protocol-unsupported"


def test_duplicate_keys_are_frame_invalid() -> None:
    line = encode_frame(_hello()).decode("utf-8")
    duplicate = line[:-2] + ',"v":1}\n'
    with pytest.raises(PairingError) as caught:
        parse_frame(duplicate.encode("utf-8"))
    assert caught.value.code == "pairing-frame-invalid"


def test_trailing_data_and_line_shape_are_frame_invalid() -> None:
    line = encode_frame(_hello())
    with pytest.raises(PairingError):
        parse_frame(line[:-1] + b"x\n")
    with pytest.raises(PairingError):
        parse_frame(line[:-1])
    with pytest.raises(PairingError):
        parse_frame(line + b"\n")
    with pytest.raises(PairingError):
        parse_frame(b"[]\n")
    with pytest.raises(PairingError):
        parse_frame(b"null\n")


def test_non_finite_numbers_are_frame_invalid() -> None:
    line = encode_frame(_hello()).decode("utf-8")
    for literal in ("NaN", "Infinity", "-Infinity"):
        malformed = line.replace('"v":1', f'"v":{literal}', 1)
        with pytest.raises(PairingError) as caught:
            parse_frame(malformed.encode("utf-8"))
        assert caught.value.code == "pairing-frame-invalid"


def test_bool_is_not_an_integer() -> None:
    assert _refusal_code(lambda: encode_frame(_hello(v=True))) == (
        "pairing-frame-invalid"
    )
    assert _refusal_code(
        lambda: encode_frame(_status_response(requested_at=True))
    ) == "pairing-frame-invalid"


def test_oversized_frame_is_frame_invalid() -> None:
    with pytest.raises(PairingError) as caught:
        parse_frame(b"{" + b"a" * (MAX_FRAME_BYTES + 1) + b"}\n")
    assert caught.value.code == "pairing-frame-invalid"


def test_unknown_frame_types_are_frame_invalid() -> None:
    assert _refusal_code(lambda: encode_frame(_hello(type="pairing.approve"))) == (
        "pairing-frame-invalid"
    )
    assert _refusal_code(lambda: encode_frame(_hello(type="pairing.device"))) == (
        "pairing-frame-invalid"
    )


def test_redeem_mode_code_exclusivity() -> None:
    with pytest.raises(PairingError) as start_without_code:
        build_redeem(
            mode="start",
            channel_binding=CHANNEL_BINDING,
            pairing_attempt_id=PAIRING_ATTEMPT_ID,
            code=None,
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=CLIENT_PUBLIC,
            permissions=["sessions.read"],
            client_proof=b"\x02" * 32,
        )
    assert start_without_code.value.code == "pairing-frame-invalid"

    with pytest.raises(PairingError) as resume_with_code:
        build_redeem(
            mode="resume",
            channel_binding=CHANNEL_BINDING,
            pairing_attempt_id=PAIRING_ATTEMPT_ID,
            code="ABCDEFGHJK",
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=CLIENT_PUBLIC,
            permissions=["sessions.read"],
            client_proof=b"\x02" * 32,
        )
    assert resume_with_code.value.code == "pairing-frame-invalid"


def test_redeem_permissions_are_closed_and_canonical() -> None:
    with pytest.raises(PairingError) as empty:
        _redeem(permissions=[])
    assert empty.value.code == "pairing-request-invalid"
    assert empty.value.field == "permissions"

    with pytest.raises(PairingError) as unknown:
        _redeem(permissions=["admin.everything"])
    assert unknown.value.code == "pairing-request-invalid"
    assert unknown.value.field == "permissions"

    frame = _redeem(
        permissions=["sessions.write", "sessions.read", "sessions.read"]
    )
    parsed = parse_frame(encode_frame(frame))
    assert parsed["permissions"] == ["sessions.read", "sessions.write"]


def test_identifier_and_text_limits() -> None:
    with pytest.raises(PairingError) as identifier:
        _redeem(device_id="d" * 257)
    assert identifier.value.code == "pairing-request-invalid"
    assert identifier.value.field == "deviceId"

    with pytest.raises(PairingError) as name:
        _redeem(name="n" * 201)
    assert name.value.code == "pairing-request-invalid"
    assert name.value.field == "name"

    with pytest.raises(PairingError) as control:
        _redeem(owner="agent\ninjected")
    assert control.value.code == "pairing-request-invalid"


def test_client_public_must_be_a_p256_point() -> None:
    with pytest.raises(PairingError) as caught:
        build_hello(
            node_id=NODE,
            host_key_fingerprint=PIN,
            client_nonce=CLIENT_NONCE,
            client_public=b"\x04" + b"\x00" * 64,
        )
    assert caught.value.code == "pairing-request-invalid"
    assert caught.value.field == "clientPublic"


def test_nonce_and_proof_lengths_are_closed() -> None:
    with pytest.raises(PairingError):
        build_hello(
            node_id=NODE,
            host_key_fingerprint=PIN,
            client_nonce=b"short",
            client_public=CLIENT_PUBLIC,
        )
    with pytest.raises(PairingError):
        _redeem(client_proof=b"\x02" * 31)
    with pytest.raises(PairingError):
        _challenge(signature=b"\x01" * 63)


def test_status_query_and_response_have_distinct_shapes() -> None:
    query = build_status_query(
        channel_binding=CHANNEL_BINDING,
        request_id=REQUEST_ID,
        pairing_attempt_id=PAIRING_ATTEMPT_ID,
        client_proof=b"\x03" * 32,
    )
    assert parse_frame(encode_frame(query)) == query

    response = _status_response()
    assert parse_frame(encode_frame(response)) == response

    with pytest.raises(PairingError):
        encode_frame({**response, "channelBinding": CHANNEL_BINDING})
    with pytest.raises(PairingError):
        encode_frame({**query, "nodeId": NODE})


def test_status_response_conditional_fields() -> None:
    with pytest.raises(PairingError):
        build_status_response(
            request_id=REQUEST_ID,
            node_id=NODE,
            fingerprint=FINGERPRINT,
            requested_at=1_000_000,
            expires_at=1_000_300,
            status="pending",
        )
    with pytest.raises(PairingError):
        build_status_response(
            request_id=REQUEST_ID,
            node_id=NODE,
            fingerprint=FINGERPRINT,
            requested_at=1_000_000,
            expires_at=1_000_300,
            status="rejected",
        )
    with pytest.raises(PairingError):
        build_status_response(
            request_id=REQUEST_ID,
            node_id=NODE,
            fingerprint=FINGERPRINT,
            requested_at=1_000_000,
            expires_at=1_000_300,
            status="approved",
        )
    with pytest.raises(PairingError):
        _status_response(device=_device())
    with pytest.raises(PairingError):
        _status_response(
            status="approved",
            device=_device(status="revoked", revokedAt=None),
        )
    with pytest.raises(PairingError):
        _status_response(
            status="approved",
            device=_device(status="revoked", revokedAt=1_000_002),
        )


def test_status_device_revocation_requires_consistent_timestamps() -> None:
    revoked = _status_response(
        status="revoked",
        device=_device(status="revoked", revokedAt=1_000_002),
    )
    parsed = parse_frame(encode_frame(revoked))
    assert parsed["status"] == "revoked"
    assert parsed["device"]["status"] == "revoked"
    assert parsed["device"]["revokedAt"] == 1_000_002

    with pytest.raises(PairingError):
        _status_response(
            status="revoked",
            device=_device(status="active", revokedAt=None),
        )


def test_pairing_error_frame_is_closed() -> None:
    frame = build_error(
        code="pairing-rate-limited",
        message="try again later",
        retry_after_seconds=60,
    )
    parsed = parse_frame(encode_frame(frame))
    assert parsed["retryAfterSeconds"] == 60

    with pytest.raises(PairingError):
        build_error(code="pairing-rate-limited", message="try again later")
    with pytest.raises(PairingError):
        build_error(
            code="pairing-internal",
            message="not leaked",
            retry_after_seconds=60,
        )
    with pytest.raises(PairingError):
        build_error(code="pairing-code-consumed", message="internal only")
