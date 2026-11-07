"""S3 TLS boundary: TLS 1.3, exact SPKI pin, no fallback.

These tests run against a real local TLS listener on an ephemeral loopback
port.  The pairing code must survive every rejected transport attempt and the
server must never receive an application frame from a failed handshake.
"""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from hyprial.shell.impl.gui.adapter.mobile.pairing import encode_frame
from hyprial.shell.impl.gui.adapter.mobile.pairing.server import (
    PairingTlsError,
    connect_pinned,
    host_key_fingerprint_from_certificate,
)
from tests.shell.gui.adapter.mobile.helpers import OPERATOR
from tests.shell.gui.adapter.mobile.pairing.server.support import (
    AuditRecorder,
    build_server,
    make_tls_material,
    new_client,
    pairing_request_count,
    redeem_once,
    tls12_client_context,
    write_rsa_material,
)


def test_tls13_exact_spki_pin(server_factory, store, tls_material):
    server = server_factory(authority=store)
    spki = tls_material.certificate.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    expected = "sha256:" + hashlib.sha256(spki).hexdigest()

    assert tls_material.fingerprint == expected
    assert server.host_key_fingerprint == expected
    assert host_key_fingerprint_from_certificate(
        tls_material.certificate
    ) == expected

    sock = connect_pinned(
        "127.0.0.1",
        server.bound_port,
        fingerprint=expected,
        trust_certfile=tls_material.certfile,
        timeout=3.0,
    )
    try:
        assert sock.version() == "TLSv1.3"
        assert sock.getpeercert(binary_form=True) == (
            tls_material.certificate.public_bytes(Encoding.DER)
        )
    finally:
        sock.close()


def test_wrong_spki_pin_fails_before_any_pairing_frame(
    server_factory, store, tls_material, other_fingerprint
):
    server = server_factory(authority=store)
    code, _expires = store.issue_pairing_code(operator=OPERATOR)

    with pytest.raises(PairingTlsError) as refusal:
        connect_pinned(
            "127.0.0.1",
            server.bound_port,
            fingerprint=other_fingerprint,
            trust_certfile=tls_material.certfile,
            timeout=3.0,
        )

    assert refusal.value.code == "pairing-tls-pin-mismatch"
    # The pinned client never returned, so it could not write a hello or a
    # code-bearing redeem frame.
    assert pairing_request_count(store) == 0
    assert redeem_once(server, tls_material, code)["status"] == "pending"


def test_test_client_requires_the_exact_host_certificate_as_trust_anchor(
    tmp_path: Path, store, tls_material
):
    """A non-pinned self-signed certificate must fail the TLS handshake.

    The test client uses ``CERT_REQUIRED`` with only the expected host
    certificate as its trust anchor.  It must not fall back to the system CA
    store, TOFU or an unverified handshake.
    """

    other = make_tls_material(tmp_path / "other")
    server = build_server(store, other)
    server.start()
    try:
        with pytest.raises(PairingTlsError) as refusal:
            connect_pinned(
                "127.0.0.1",
                server.bound_port,
                fingerprint=tls_material.fingerprint,
                trust_certfile=tls_material.certfile,
                timeout=3.0,
            )
        assert refusal.value.code == "pairing-tls-handshake-failed"
        assert pairing_request_count(store) == 0
    finally:
        server.close()


def test_tls12_client_is_rejected_without_sending_a_frame(
    server_factory, store, tls_material, audit_recorder: AuditRecorder
):
    server = server_factory(authority=store)
    code, _expires = store.issue_pairing_code(operator=OPERATOR)

    context = tls12_client_context(tls_material.certfile)
    raw = socket.create_connection(
        ("127.0.0.1", server.bound_port), timeout=3.0
    )
    try:
        with pytest.raises(OSError):
            context.wrap_socket(raw, server_hostname=None)
    finally:
        raw.close()

    failure = audit_recorder.wait_for("pairing-tls-failed")[-1]
    assert failure["errorCode"] == "pairing-tls-handshake-failed"
    assert pairing_request_count(store) == 0
    assert redeem_once(server, tls_material, code)["status"] == "pending"


def test_plaintext_client_is_rejected_without_sending_a_frame(
    server_factory, store, tls_material, audit_recorder: AuditRecorder,
    raw_socket,
):
    server = server_factory(authority=store)
    code, _expires = store.issue_pairing_code(operator=OPERATOR)
    client, _public = new_client(tls_material)

    raw = raw_socket(server)
    try:
        raw.sendall(encode_frame(client.hello()))
    except OSError:
        pass
    raw.close()

    failure = audit_recorder.wait_for("pairing-tls-failed")[-1]
    assert failure["errorCode"] == "pairing-tls-handshake-failed"
    assert pairing_request_count(store) == 0
    assert redeem_once(server, tls_material, code)["status"] == "pending"


def test_host_material_must_be_p256_and_matching(
    tmp_path: Path, store, tls_material
):
    rsa_cert, rsa_key = write_rsa_material(tmp_path / "rsa")
    with pytest.raises(PairingTlsError) as rsa_refusal:
        build_server(store, tls_material, certfile=rsa_cert, keyfile=rsa_key)
    assert rsa_refusal.value.code == "pairing-tls-material-invalid"

    other = make_tls_material(tmp_path / "other")
    with pytest.raises(PairingTlsError) as mismatch:
        build_server(
            store,
            tls_material,
            certfile=other.certfile,
            keyfile=tls_material.keyfile,
        )
    assert mismatch.value.code == "pairing-tls-material-invalid"


def test_missing_material_fails_at_construction(
    tmp_path: Path, store, tls_material
):
    with pytest.raises(PairingTlsError) as refusal:
        build_server(
            store,
            tls_material,
            certfile=tmp_path / "missing.crt",
            keyfile=tmp_path / "missing.key",
        )
    assert refusal.value.code == "pairing-tls-material-invalid"


def test_non_loopback_listener_is_refused(store, tls_material):
    with pytest.raises(ValueError):
        build_server(store, tls_material, host="0.0.0.0")


def test_derive_key_must_be_p256(store, tls_material):
    with pytest.raises((ValueError, TypeError)):
        build_server(
            store,
            tls_material,
            host_derive_key=object(),
        )
