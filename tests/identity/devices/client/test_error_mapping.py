"""Error-envelope mapping, replay semantics and load-bearing counter-tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.interface import (
    DeviceAuthorityConfig,
    DeviceTransportClient,
    TransportClientError,
    TransportCredentialError,
)

from ..binding import support as binding_support
from . import support


def _config(server, *, client_tls=None, ca=None, **overrides):
    values = dict(
        origin=server.origin,
        server_name="127.0.0.1",
        trust_bundle_pem=(ca or server.ca).pem,
        connect_timeout=2.0,
        handshake_timeout=2.0,
        read_timeout=2.0,
        write_timeout=2.0,
        client_tls=client_tls,
    )
    values.update(overrides)
    return DeviceAuthorityConfig(**values)


def _staged(tmp_path: Path):
    return binding_support.stage(tmp_path)


ERROR_CASES = [
    (400, "BAD_REQUEST", False, "REJECTED"),
    (400, "CSR_INVALID", False, "REJECTED"),
    (401, "TOKEN_INVALID", False, "REJECTED"),
    (401, "DEVICE_AUTH_REQUIRED", False, "REJECTED"),
    (401, "CREDENTIAL_EXPIRED", False, "REJECTED"),
    (401, "CREDENTIAL_REAUTH_REQUIRED", False, "REJECTED"),
    (403, "DEVICE_AUTH_MISMATCH", False, "REJECTED"),
    (403, "DEVICE_REVOKED", False, "REJECTED"),
    (403, "DEVICE_OWNER_MISMATCH", False, "REJECTED"),
    (403, "OWNER_DISABLED", False, "REJECTED"),
    (409, "DEVICE_BINDING_CONFLICT", False, "REJECTED"),
    (409, "IDEMPOTENCY_CONFLICT", False, "REJECTED"),
    (409, "KEY_GENERATION_STALE", False, "REJECTED"),
    (409, "KEY_GENERATION_EXHAUSTED", False, "REJECTED"),
    (413, "REQUEST_TOO_LARGE", False, "REJECTED"),
    (431, "REQUEST_HEADER_TOO_LARGE", False, "REJECTED"),
    (503, "AUTHORITY_UNAVAILABLE", True, "UNKNOWN"),
    (503, "ISSUER_INVALID", False, "REJECTED"),
    (503, "UNSAFE_AUTHORITY_STATE", False, "REJECTED"),
    (503, "AUTHORITY_ISSUER_MISMATCH", False, "REJECTED"),
    (503, "AUTHORITY_CONFIG_INVALID", False, "REJECTED"),
]


@pytest.mark.parametrize(("status", "code", "retryable", "outcome"), ERROR_CASES)
def test_each_authority_error_is_mapped_verbatim(
    tmp_path: Path, status: int, code: str, retryable: bool, outcome: str
) -> None:
    staged = _staged(tmp_path)
    operation_id = staged.operation_id
    envelope = support.error_envelope(operation_id, code, retryable)
    with support.LoopbackAuthority(
        [
            lambda _request: support.raw_response(
                envelope, status=f"{status} ERROR"
            )
        ]
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    error = caught.value
    assert error.code == code
    assert error.retryable is retryable
    assert error.outcome == outcome
    assert error.http_status == status
    assert error.operation_id == operation_id
    assert str(error) == code
    assert "BEGIN" not in repr(error)


@pytest.mark.parametrize(
    "envelope_factory",
    [
        pytest.param(
            lambda operation_id: support.error_envelope(
                operation_id, "REQUEST_TOO_LARGE", True
            ),
            id="retryable-flag-mismatch",
        ),
        pytest.param(
            lambda operation_id: support.error_envelope(
                operation_id, "UNKNOWN_CODE", False
            ),
            id="unknown-code",
        ),
        pytest.param(
            lambda operation_id: json.dumps(
                {
                    "ok": False,
                    "version": support.ENVELOPE_VERSION,
                    "operationId": operation_id,
                    "code": "BAD_REQUEST",
                    "retryable": False,
                    "data": {"leak": "value"},
                },
                separators=(",", ":"),
            ).encode(),
            id="error-data-not-null",
        ),
        pytest.param(
            lambda operation_id: json.dumps(
                {
                    "ok": False,
                    "version": support.ENVELOPE_VERSION,
                    "operationId": operation_id,
                    "code": "BAD_REQUEST",
                    "retryable": False,
                    "data": None,
                    "extra": True,
                },
                separators=(",", ":"),
            ).encode(),
            id="extra-envelope-key",
        ),
        pytest.param(
            lambda operation_id: support.error_envelope(
                "11111111-1111-4111-8111-111111111111", "BAD_REQUEST", False
            ),
            id="wrong-operation-id",
        ),
        pytest.param(
            lambda operation_id: json.dumps(
                {
                    "ok": False,
                    "version": "hyprial.transport.v2",
                    "operationId": operation_id,
                    "code": "BAD_REQUEST",
                    "retryable": False,
                    "data": None,
                },
                separators=(",", ":"),
            ).encode(),
            id="wrong-version",
        ),
    ],
)
def test_protocol_invalid_error_envelopes_are_unknown_outcomes(
    tmp_path: Path, envelope_factory
) -> None:
    staged = _staged(tmp_path)
    envelope = envelope_factory(staged.operation_id)
    with support.LoopbackAuthority(
        [lambda _request: support.raw_response(envelope, status="400 ERROR")]
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"
    assert caught.value.outcome == "UNKNOWN"
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    ("response_factory", "expected_code"),
    [
        pytest.param(
            lambda body: support.raw_response(body, status="302 Found"),
            "TRANSPORT_REDIRECT_REFUSED",
            id="redirect",
        ),
        pytest.param(
            lambda body: support.raw_response(
                body, extra_headers=(("Transfer-Encoding", "chunked"),)
            ),
            "TRANSPORT_RESPONSE_INVALID",
            id="chunked",
        ),
        pytest.param(
            lambda body: support.raw_response(
                body, extra_headers=(("Content-Encoding", "gzip"),)
            ),
            "TRANSPORT_RESPONSE_INVALID",
            id="compressed",
        ),
        pytest.param(
            lambda body: support.raw_response(body, connection="keep-alive"),
            "TRANSPORT_RESPONSE_INVALID",
            id="keep-alive",
        ),
        pytest.param(
            lambda body: (
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n"
                b"Connection: close\r\n\r\n" + body
            ),
            "TRANSPORT_RESPONSE_INVALID",
            id="duplicate-content-length",
        ),
        pytest.param(
            lambda body: (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\n\r\n" + body
            ),
            "TRANSPORT_RESPONSE_INVALID",
            id="missing-content-length",
        ),
        pytest.param(
            lambda body: (
                b"HTTP/1.1 200 OK\r\nContent-Length: 0"
                b"0\r\nConnection: close\r\n\r\n" + body
            ),
            "TRANSPORT_RESPONSE_INVALID",
            id="noncanonical-content-length",
        ),
        pytest.param(
            lambda body: (
                b"HTTP/1.0 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            ),
            "TRANSPORT_RESPONSE_INVALID",
            id="http-version",
        ),
    ],
)
def test_framing_violations_are_refused(
    tmp_path: Path, response_factory, expected_code: str
) -> None:
    staged = _staged(tmp_path)
    body = support.success_envelope(
        staged.operation_id, support.certificate_data()
    )
    with support.LoopbackAuthority(
        [lambda _request: response_factory(body)]
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    assert caught.value.code == expected_code


def test_success_envelope_data_must_be_the_exact_interface_a(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    body = support.success_envelope(
        staged.operation_id, support.certificate_data(extra="field")
    )
    with support.LoopbackAuthority([lambda _request: support.raw_response(body)]) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"


def test_retire_success_data_must_match_the_request(tmp_path: Path) -> None:
    operation_id = "11111111-1111-4111-8111-111111111111"
    data = {
        "retired": True,
        "remoteEnforcementState": "pending",
        "operationId": "22222222-2222-4222-8222-222222222222",
    }
    with support.LoopbackAuthority(
        [lambda _request: support.raw_response(support.success_envelope(operation_id, data))]
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).retire(
                operation_id=operation_id,
                binding_id="33333333-3333-4333-8333-333333333333",
                expected_credential_generation=1,
            )
    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"


def test_exact_replay_after_lost_response_uses_same_bytes(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    operation_id = staged.operation_id
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [
            lambda _request: None,
            lambda _request: support.raw_response(
                support.success_envelope(operation_id, data)
            ),
        ],
        max_sessions=2,
    ) as server:
        client = DeviceTransportClient(_config(server))
        with pytest.raises(TransportClientError) as caught:
            client.enroll(staged)
        assert caught.value.outcome == "UNKNOWN"
        assert client.enroll(staged) == data
    assert len(server.observation.requests) == 2
    assert server.observation.requests[0] == server.observation.requests[1]


def test_changed_body_with_the_same_operation_id_is_refused(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    operation_id = staged.operation_id
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [
            lambda _request: None,
            lambda _request: support.raw_response(
                support.success_envelope(operation_id, data)
            ),
        ],
        max_sessions=2,
    ) as server:
        client = DeviceTransportClient(_config(server))
        with pytest.raises(TransportClientError):
            client.enroll(staged)
        with pytest.raises(TransportClientError) as caught:
            client.retire(
                operation_id=operation_id,
                binding_id="33333333-3333-4333-8333-333333333333",
                expected_credential_generation=1,
            )
        assert caught.value.code == "TRANSPORT_REPLAY_MISMATCH"
        assert caught.value.outcome == "NOT_SENT"
        assert client.enroll(staged) == data
    assert server.observation.connections == 2


def test_a_new_operation_cannot_skip_an_unresolved_one(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [
            lambda _request: None,
            lambda _request: support.raw_response(
                support.success_envelope(staged.operation_id, data)
            ),
        ],
        max_sessions=2,
    ) as server:
        client = DeviceTransportClient(_config(server))
        with pytest.raises(TransportClientError):
            client.enroll(staged)
        with pytest.raises(TransportClientError) as caught:
            client.request_certificate(
                operation_id="44444444-4444-4444-8444-444444444444",
                device_uid=staged.device_uid,
                expected_credential_generation=0,
                csr_pem=staged.csr_pem,
            )
        assert caught.value.code == "TRANSPORT_OUTCOME_UNRESOLVED"
        assert caught.value.outcome == "UNKNOWN"
        assert client.enroll(staged) == data
    assert server.observation.connections == 2


def test_mutation_removing_uri_cardinality_check_accepts_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    from hyprial.identity.impl.device import transport_credential as credential_module
    from hyprial.identity.interface import validate_enrollment_response

    staged = _staged(tmp_path)
    claims = binding_support.claims_for(
        binding_id="11111111-1111-4111-8111-111111111111",
        device_uid=staged.device_uid,
        credential_generation=1,
        spki_sha256=staged.spki_sha256,
        renewable_until=binding_support.horizon(),
    )
    response, bundle = binding_support.binding_response(
        staged,
        claims=claims,
        uri_sans=(
            binding_support.reference_uri(claims),
            binding_support.reference_uri(claims),
        ),
    )
    with pytest.raises(TransportCredentialError):
        validate_enrollment_response(
            response,
            staged=staged,
            pinned_trust_bundle_pem=bundle,
            effective_owner=binding_support.OWNER,
        )
    monkeypatch.setattr(
        credential_module, "verify_credential_binding", lambda *_args: None
    )
    credential = validate_enrollment_response(
        response,
        staged=staged,
        pinned_trust_bundle_pem=bundle,
        effective_owner=binding_support.OWNER,
    )
    assert credential.binding_id == claims["bindingId"]


def test_mutation_removing_jcs_digest_check_accepts_a_foreign_uri(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib

    from hyprial.identity.impl.device.binding import profile
    from hyprial.identity.interface import validate_enrollment_response

    staged = _staged(tmp_path)
    claims = binding_support.claims_for(
        binding_id="11111111-1111-4111-8111-111111111111",
        device_uid=staged.device_uid,
        credential_generation=1,
        spki_sha256=staged.spki_sha256,
        renewable_until=binding_support.horizon(),
    )
    foreign_digest = hashlib.sha256(b"{}").hexdigest()
    response, bundle = binding_support.binding_response(
        staged,
        claims=claims,
        uri_sans=(binding_support.BINDING_URI_PREFIX + foreign_digest,),
    )
    with pytest.raises(TransportCredentialError):
        validate_enrollment_response(
            response,
            staged=staged,
            pinned_trust_bundle_pem=bundle,
            effective_owner=binding_support.OWNER,
        )
    monkeypatch.setattr(profile, "canonical_binding_bytes", lambda _claims: b"{}")
    credential = validate_enrollment_response(
        response,
        staged=staged,
        pinned_trust_bundle_pem=bundle,
        effective_owner=binding_support.OWNER,
    )
    assert credential.binding_id == claims["bindingId"]


def test_mutation_emitting_a_second_request_turns_the_single_request_witness_red(
    tmp_path: Path, monkeypatch
) -> None:
    from hyprial.identity.impl.device.client import http as client_http

    staged = _staged(tmp_path)
    data = support.certificate_data()
    original = client_http._request_bytes

    def duplicated(config, route, body):
        request = original(config, route, body)
        return request + request

    monkeypatch.setattr(client_http, "_request_bytes", duplicated)
    with support.LoopbackAuthority(
        [
            lambda _request: support.raw_response(
                support.success_envelope(staged.operation_id, data)
            )
        ]
    ) as server:
        DeviceTransportClient(_config(server)).enroll(staged)
    assert server.observation.extra_after_response > 0
    assert len(server.observation.requests) == 2
