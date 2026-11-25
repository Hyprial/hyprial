"""Use M1-committed material for a real, local, one-request mTLS exchange.

The positive case observes the peer certificate and exact retirement request.
TLS controls reject absent/untrusted client certificates and an untrusted server.
Expiry and unsafe storage controls reject at the M1 reader before networking.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from hyprial.identity.impl.device import transport_credential
from hyprial.identity.interface import (
    RETIRE_BINDING_ROUTE,
    DeviceAuthorityConfig,
    DeviceTransportClient,
    TransportClientError,
    commit_transport_credential,
    load_client_tls_material,
    read_transport_credential,
    validate_enrollment_response,
)

from ..binding import support as binding_support
from ..client import support as client_support


def _committed(home: Path, ca):
    staged = binding_support.stage(home)
    claims = binding_support.claims_for(
        binding_id=str(uuid4()),
        device_uid=staged.device_uid,
        credential_generation=1,
        spki_sha256=staged.spki_sha256,
        renewable_until=binding_support.horizon(),
    )
    response, bundle = binding_support.binding_response(staged, ca=ca, claims=claims)
    credential = validate_enrollment_response(
        response,
        staged=staged,
        pinned_trust_bundle_pem=bundle,
        effective_owner=binding_support.OWNER,
    )
    commit_transport_credential(
        home, staged=staged, credential=credential, pinned_trust_bundle_pem=bundle
    )
    before = binding_support.inventory(home)
    assert read_transport_credential(home) == credential
    material = load_client_tls_material(home)
    assert binding_support.inventory(home) == before
    expected_chain = (credential.certificate_pem + credential.chain_pem).encode("ascii")
    assert hashlib.sha256(material.certificate_chain_pem).digest() == hashlib.sha256(
        expected_chain
    ).digest()
    assert hashlib.sha256(material.trust_bundle_pem).digest() == hashlib.sha256(
        bundle
    ).digest()
    return credential, material


def _config(server, material, *, include_certificate=True):
    return DeviceAuthorityConfig(
        origin=server.origin,
        server_name="127.0.0.1",
        trust_bundle_pem=material.trust_bundle_pem,
        connect_timeout=2,
        handshake_timeout=2,
        read_timeout=2,
        write_timeout=2,
        client_tls=material if include_certificate else None,
    )


def _retire(client, credential, operation_id):
    return client.retire(
        operation_id=operation_id,
        binding_id=credential.binding_id,
        expected_credential_generation=credential.credential_generation,
    )


def test_committed_material_authenticates_exact_single_request(tmp_path: Path) -> None:
    shared_ca = binding_support.make_ca()
    credential, material = _committed(tmp_path, shared_ca)
    before = binding_support.inventory(tmp_path)
    operation_id = str(uuid4())
    data = {
        "retired": True,
        "remoteEnforcementState": "pending",
        "operationId": operation_id,
    }
    with client_support.LoopbackAuthority(
        [lambda _request: client_support.raw_response(
            client_support.success_envelope(operation_id, data)
        )],
        ca=shared_ca,
        require_client_cert=True,
    ) as server:
        result = _retire(DeviceTransportClient(_config(server, material)), credential, operation_id)
    assert result == data
    observation = server.observation
    assert observation.connections == 1
    assert observation.errors == []
    assert observation.client_certificate_der is not None
    peer = x509.load_der_x509_certificate(observation.client_certificate_der)
    committed_leaf = x509.load_pem_x509_certificate(credential.certificate_pem.encode("ascii"))
    assert peer.fingerprint(hashes.SHA256()) == committed_leaf.fingerprint(hashes.SHA256())
    body = json.dumps(
        {
            "version": client_support.ENVELOPE_VERSION,
            "operationId": operation_id,
            "bindingId": credential.binding_id,
            "expectedCredentialGeneration": credential.credential_generation,
        },
        separators=(",", ":"),
    ).encode()
    expected = (
        f"POST {RETIRE_BINDING_ROUTE} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body
    assert observation.requests == [expected]
    assert observation.extra_after_response == 0
    assert binding_support.inventory(tmp_path) == before


@pytest.mark.parametrize("control", ["no-client-certificate", "wrong-client-ca", "wrong-server-ca"])
def test_committed_material_tls_rejection_controls(tmp_path: Path, control: str) -> None:
    shared_ca = binding_support.make_ca()
    credential, material = _committed(tmp_path, shared_ca)
    before = binding_support.inventory(tmp_path)
    other_ca = binding_support.make_ca("other root")
    with client_support.LoopbackAuthority(
        [],
        ca=other_ca if control == "wrong-client-ca" else shared_ca,
        server_ca=other_ca if control == "wrong-server-ca" else shared_ca,
        require_client_cert=True,
    ) as server:
        config = _config(server, material, include_certificate=control != "no-client-certificate")
        with pytest.raises(TransportClientError) as caught:
            _retire(DeviceTransportClient(config), credential, str(uuid4()))
    error = caught.value
    if control == "wrong-server-ca":
        assert error.code == "TRANSPORT_CHAIN_UNTRUSTED"
        assert error.outcome == "NOT_SENT"
        assert error.retryable is False
    else:
        # TLS 1.3 can report the server's rejection after the client handshake.
        assert error.code in {"AUTHORITY_UNAVAILABLE", "TRANSPORT_RESPONSE_UNKNOWN"}
        assert error.outcome in {"NOT_SENT", "UNKNOWN"}
        assert error.retryable is True
    assert error.__context__ is None
    assert error.__cause__ is None
    assert server.observation.connections == 1
    assert server.observation.client_certificate_der is None
    assert server.observation.requests == []
    assert server.observation.errors == ["handshake"]
    assert binding_support.inventory(tmp_path) == before


def test_expired_committed_material_is_rejected_before_networking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_ca = binding_support.make_ca()
    credential, _material = _committed(tmp_path, shared_ca)
    before = binding_support.inventory(tmp_path)
    expired_at = datetime.fromtimestamp(credential.not_after, timezone.utc) + timedelta(seconds=1)

    class AfterExpiry(datetime):
        @classmethod
        def now(cls, tz=None):
            return expired_at.astimezone(tz)

    with client_support.LoopbackAuthority(
        [], ca=shared_ca, require_client_cert=True, accept_timeout=0.3
    ) as server:
        monkeypatch.setattr(transport_credential, "datetime", AfterExpiry)
        assert read_transport_credential(tmp_path) is None
        with pytest.raises(TransportClientError) as caught:
            load_client_tls_material(tmp_path)
    assert caught.value.code == "TRANSPORT_CREDENTIAL_ABSENT"
    assert caught.value.outcome == "NOT_SENT"
    assert caught.value.retryable is False
    assert server.observation.connections == 0
    assert server.observation.requests == []
    assert server.observation.errors == ["no-connection"]
    assert binding_support.inventory(tmp_path) == before


def test_unsafe_committed_material_is_rejected_before_networking(tmp_path: Path) -> None:
    shared_ca = binding_support.make_ca()
    _credential, _material = _committed(tmp_path, shared_ca)
    (tmp_path / "secrets/transport/device-key.pem").chmod(0o640)
    before = binding_support.inventory(tmp_path)
    with client_support.LoopbackAuthority(
        [], ca=shared_ca, require_client_cert=True, accept_timeout=0.3
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            load_client_tls_material(tmp_path)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert caught.value.outcome == "NOT_SENT"
    assert caught.value.retryable is False
    assert server.observation.connections == 0
    assert server.observation.requests == []
    assert server.observation.errors == ["no-connection"]
    assert binding_support.inventory(tmp_path) == before
