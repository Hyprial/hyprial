"""Socket-level witnesses for the one-request device HTTP client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.impl.device.client import http as client_http
from hyprial.identity.interface import (
    DEVICE_CERTIFICATE_ROUTE,
    MAX_REQUEST_BYTES,
    RETIRE_BINDING_ROUTE,
    ClientTlsMaterial,
    DeviceAuthorityConfig,
    DeviceTransportClient,
    TransportClientError,
    commit_transport_credential,
    load_client_tls_material,
    validate_enrollment_response,
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


def _expected_body(staged, operation_id: str) -> bytes:
    return json.dumps(
        {
            "version": support.ENVELOPE_VERSION,
            "operationId": operation_id,
            "deviceUid": staged.device_uid,
            "expectedCredentialGeneration": staged.expected_credential_generation,
            "csrPem": staged.csr_pem,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _expected_request(server, route: str, body: bytes) -> bytes:
    return (
        f"POST {route} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode() + body


def test_exact_single_request_framing_and_success(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    operation_id = staged.operation_id
    body = _expected_body(staged, operation_id)
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [lambda _request: support.raw_response(support.success_envelope(operation_id, data))]
    ) as server:
        result = DeviceTransportClient(_config(server)).enroll(staged)
    assert result == data
    assert server.observation.connections == 1
    assert server.observation.requests == [
        _expected_request(server, DEVICE_CERTIFICATE_ROUTE, body)
    ]
    assert server.observation.extra_after_response == 0
    assert server.observation.errors == []


def test_retire_route_framing_and_success_data(tmp_path: Path) -> None:
    operation_id = "11111111-1111-4111-8111-111111111111"
    data = {
        "retired": True,
        "remoteEnforcementState": "pending",
        "operationId": operation_id,
    }
    with support.LoopbackAuthority(
        [lambda _request: support.raw_response(support.success_envelope(operation_id, data))]
    ) as server:
        result = DeviceTransportClient(_config(server)).retire(
            operation_id=operation_id,
            binding_id="22222222-2222-4222-8222-222222222222",
            expected_credential_generation=3,
        )
    assert result == data
    body = json.dumps(
        {
            "version": support.ENVELOPE_VERSION,
            "operationId": operation_id,
            "bindingId": "22222222-2222-4222-8222-222222222222",
            "expectedCredentialGeneration": 3,
        },
        separators=(",", ":"),
    ).encode()
    assert server.observation.requests == [
        _expected_request(server, RETIRE_BINDING_ROUTE, body)
    ]
    assert server.observation.extra_after_response == 0


def test_response_oversize_is_unknown_outcome(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    operation_id = staged.operation_id
    with support.LoopbackAuthority(
        [
            lambda _request: support.raw_response(
                b"x" * 65537, content_length=65537
            )
        ]
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    error = caught.value
    assert error.code == "TRANSPORT_RESPONSE_UNKNOWN"
    assert error.retryable is True
    assert error.outcome == "UNKNOWN"
    assert error.operation_id == operation_id
    assert server.observation.connections == 1


def test_short_body_is_unknown_outcome(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    body = support.success_envelope(staged.operation_id, support.certificate_data())
    with support.LoopbackAuthority(
        [lambda _request: support.raw_response(body[:50], content_length=len(body))]
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    assert caught.value.code == "TRANSPORT_RESPONSE_UNKNOWN"
    assert caught.value.outcome == "UNKNOWN"


def test_lost_response_is_unknown_outcome(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    with support.LoopbackAuthority([lambda _request: None]) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    assert caught.value.code == "TRANSPORT_RESPONSE_UNKNOWN"
    assert caught.value.outcome == "UNKNOWN"
    assert caught.value.retryable is True
    assert server.observation.requests


def test_extra_second_response_bytes_are_rejected(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    first = support.raw_response(
        support.success_envelope(staged.operation_id, support.certificate_data())
    )
    second = support.success_envelope(staged.operation_id, support.certificate_data())
    with support.LoopbackAuthority([lambda _request: first + second]) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"
    assert caught.value.outcome == "UNKNOWN"
    assert len(server.observation.requests) == 1


def test_untrusted_server_certificate_is_refused(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    other_ca = binding_support.make_ca("untrusted root")
    with support.LoopbackAuthority(
        [lambda _request: None], ca=other_ca, server_ca=other_ca
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(
                _config(server, ca=binding_support.make_ca("pinned root"))
            ).enroll(staged)
    assert caught.value.code == "TRANSPORT_CHAIN_UNTRUSTED"
    assert caught.value.outcome == "NOT_SENT"
    assert caught.value.retryable is False
    assert server.observation.requests == []


def test_device_route_requires_client_certificate(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    operation_id = staged.operation_id
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [lambda _request: support.raw_response(support.success_envelope(operation_id, data))],
        require_client_cert=True,
    ) as server:
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
        assert caught.value.outcome in {"NOT_SENT", "UNKNOWN"}
        assert caught.value.retryable is True
        assert server.observation.requests == []
    with support.LoopbackAuthority(
        [lambda _request: support.raw_response(support.success_envelope(operation_id, data))],
        require_client_cert=True,
    ) as server:
        result = DeviceTransportClient(
            _config(server, client_tls=support.client_material(server.ca))
        ).enroll(staged)
        assert result == data
        assert server.observation.client_certificate_der
        assert server.observation.requests


def test_local_request_size_and_identity_validation(tmp_path: Path) -> None:
    staged = _staged(tmp_path)
    with support.LoopbackAuthority([], accept_timeout=0.3) as server:
        client = DeviceTransportClient(_config(server))
        with pytest.raises(TransportClientError) as caught:
            client.request_certificate(
                operation_id=staged.operation_id,
                device_uid=staged.device_uid,
                expected_credential_generation=0,
                csr_pem="A" * (MAX_REQUEST_BYTES + 1),
            )
        assert caught.value.code == "TRANSPORT_REQUEST_TOO_LARGE"
        with pytest.raises(TransportClientError) as caught:
            client.retire(
                operation_id="not-a-uuid",
                binding_id="22222222-2222-4222-8222-222222222222",
                expected_credential_generation=1,
            )
        assert caught.value.code == "TRANSPORT_REQUEST_INVALID"
        with pytest.raises(TransportClientError) as caught:
            client.request_certificate(
                operation_id=staged.operation_id,
                device_uid=staged.device_uid,
                expected_credential_generation=True,
                csr_pem=staged.csr_pem,
            )
        assert caught.value.code == "TRANSPORT_REQUEST_INVALID"
    assert server.observation.connections == 0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"origin": "http://127.0.0.1:8443"}, id="plain-http"),
        pytest.param({"origin": "https://127.0.0.1:8443/path"}, id="path"),
        pytest.param({"origin": "https://user@127.0.0.1:8443"}, id="userinfo"),
        pytest.param({"origin": "https://127.0.0.1:8443?x=1"}, id="query"),
        pytest.param({"origin": "https://127.0.0.1:0"}, id="zero-port"),
        pytest.param({"origin": "https://127.0.0.1:70000"}, id="port-overflow"),
        pytest.param({"server_name": "authority.test"}, id="sni-mismatch"),
        pytest.param({"connect_timeout": 0}, id="zero-connect-timeout"),
        pytest.param({"read_timeout": -1}, id="negative-read-timeout"),
        pytest.param({"write_timeout": float("inf")}, id="infinite-write-timeout"),
    ],
)
def test_origin_and_timeouts_are_validated(tmp_path: Path, overrides) -> None:
    with support.LoopbackAuthority([], accept_timeout=0.3) as server:
        values = dict(
            origin=server.origin,
            server_name="127.0.0.1",
            trust_bundle_pem=server.ca.pem,
            connect_timeout=2.0,
            handshake_timeout=2.0,
            read_timeout=2.0,
            write_timeout=2.0,
        )
        values.update(overrides)
        with pytest.raises(TransportClientError) as caught:
            DeviceAuthorityConfig(**values)
    assert caught.value.code == "TRANSPORT_CLIENT_CONFIG_INVALID"
    assert caught.value.outcome == "NOT_SENT"


def test_proxy_environment_is_ignored(tmp_path: Path, monkeypatch) -> None:
    staged = _staged(tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [
            lambda _request: support.raw_response(
                support.success_envelope(staged.operation_id, data)
            )
        ]
    ) as server:
        assert DeviceTransportClient(_config(server)).enroll(staged) == data


POST_SEND_OPERATION = "33333333-3333-4333-8333-333333333333"
POST_SEND_SECOND_OPERATION = "44444444-4444-4444-8444-444444444444"
POST_SEND_BINDING = "55555555-5555-4555-8555-555555555555"
POST_SEND_SECRET = "/secret/path Authorization: Bearer header-secret"


class _RawSocket:
    def settimeout(self, _value) -> None:
        pass

    def close(self) -> None:
        pass


class _InjectedTls:
    def __init__(self, site: str, wire: bytes) -> None:
        self.site = site
        self._chunks = [wire, b""]
        self.sent = 0

    def settimeout(self, _value) -> None:
        if self.sent and self.site == "read-timeout":
            raise OSError(POST_SEND_SECRET)

    def sendall(self, _request: bytes) -> None:
        self.sent += 1

    def recv(self, _size: int) -> bytes:
        if self.site == "buffer":
            return b"x"
        return self._chunks.pop(0)

    def close(self) -> None:
        pass


class _InjectedContext:
    def __init__(self, tls: _InjectedTls) -> None:
        self._tls = tls

    def wrap_socket(self, *_args, **_kwargs) -> _InjectedTls:
        return self._tls


class _FailingBuffer:
    def __iadd__(self, _chunk):
        raise MemoryError(POST_SEND_SECRET)

    def __len__(self) -> int:
        return 0


def _post_send_wire() -> bytes:
    body = support.success_envelope(
        POST_SEND_OPERATION,
        {
            "retired": True,
            "remoteEnforcementState": "synthetic-local-result",
            "operationId": POST_SEND_OPERATION,
        },
    )
    return support.raw_response(body)


def _install_post_send_injection(monkeypatch, site: str) -> _InjectedTls:
    tls = _InjectedTls(site, _post_send_wire())
    monkeypatch.setattr(
        client_http, "_client_context", lambda _config: _InjectedContext(tls)
    )
    monkeypatch.setattr(
        client_http.socket,
        "create_connection",
        lambda *_args, **_kwargs: _RawSocket(),
    )
    if site == "buffer":
        monkeypatch.setattr(
            client_http, "bytearray", _FailingBuffer, raising=False
        )
    elif site == "json":
        def _fail_json(*_args, **_kwargs):
            raise RuntimeError(POST_SEND_SECRET)

        monkeypatch.setattr(client_http.json, "loads", _fail_json)
    return tls


def _retire(client: DeviceTransportClient, operation_id: str):
    return client.retire(
        operation_id=operation_id,
        binding_id=POST_SEND_BINDING,
        expected_credential_generation=1,
    )


@pytest.mark.parametrize(
    ("site", "exception_class"),
    [
        pytest.param("buffer", "MemoryError", id="response-buffer"),
        pytest.param("read-timeout", "OSError", id="read-settimeout"),
        pytest.param("json", "RuntimeError", id="generic-json-boundary"),
    ],
)
def test_post_send_exception_boundary_is_typed_redacted_and_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    site: str,
    exception_class: str,
) -> None:
    tls = _install_post_send_injection(monkeypatch, site)
    client = DeviceTransportClient(
        DeviceAuthorityConfig(
            origin="https://authority.test",
            server_name="authority.test",
            trust_bundle_pem=b"synthetic",
            connect_timeout=1,
            handshake_timeout=1,
            read_timeout=1,
            write_timeout=1,
        )
    )
    with pytest.raises(TransportClientError) as caught:
        _retire(client, POST_SEND_OPERATION)
    error = caught.value
    assert tls.sent == 1
    assert error.code == "TRANSPORT_RESPONSE_INVALID"
    assert error.outcome == "UNKNOWN"
    assert error.retryable is True
    assert error.reason_code == "POST_SEND_EXCEPTION"
    assert error.exception_class == exception_class
    assert error.__context__ is None
    assert error.__cause__ is None
    rendered = f"{error!s}\n{error!r}\n" + "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert "/secret/path" not in rendered
    assert "header-secret" not in rendered
    assert "Authorization" not in rendered
    with pytest.raises(TransportClientError) as unresolved:
        _retire(client, POST_SEND_SECOND_OPERATION)
    assert unresolved.value.code == "TRANSPORT_OUTCOME_UNRESOLVED"
    assert unresolved.value.outcome == "UNKNOWN"
    assert tls.sent == 1


def test_pre_send_connect_failure_keeps_not_sent_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tls = _InjectedTls("unused", b"")
    monkeypatch.setattr(
        client_http, "_client_context", lambda _config: _InjectedContext(tls)
    )

    def _fail_connect(*_args, **_kwargs):
        raise OSError(POST_SEND_SECRET)

    monkeypatch.setattr(client_http.socket, "create_connection", _fail_connect)
    client = DeviceTransportClient(
        DeviceAuthorityConfig(
            origin="https://authority.test",
            server_name="authority.test",
            trust_bundle_pem=b"synthetic",
            connect_timeout=1,
            handshake_timeout=1,
            read_timeout=1,
            write_timeout=1,
        )
    )
    with pytest.raises(TransportClientError) as caught:
        _retire(client, POST_SEND_OPERATION)
    assert caught.value.code == "AUTHORITY_UNAVAILABLE"
    assert caught.value.outcome == "NOT_SENT"
    assert caught.value.retryable is True
    assert tls.sent == 0
    assert POST_SEND_SECRET not in str(caught.value)
    assert POST_SEND_SECRET not in repr(caught.value)


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_post_send_control_flow_exceptions_propagate(
    monkeypatch: pytest.MonkeyPatch, signal: type[BaseException]
) -> None:
    tls = _install_post_send_injection(monkeypatch, "json")

    def _raise_signal(*_args, **_kwargs):
        raise signal(POST_SEND_SECRET)

    monkeypatch.setattr(client_http.json, "loads", _raise_signal)
    client = DeviceTransportClient(
        DeviceAuthorityConfig(
            origin="https://authority.test",
            server_name="authority.test",
            trust_bundle_pem=b"synthetic",
            connect_timeout=1,
            handshake_timeout=1,
            read_timeout=1,
            write_timeout=1,
        )
    )
    with pytest.raises(signal):
        _retire(client, POST_SEND_OPERATION)
    assert tls.sent == 1


def _committed_home(tmp_path: Path):
    staged = _staged(tmp_path)
    claims = binding_support.claims_for(
        binding_id="11111111-1111-4111-8111-111111111111",
        device_uid=staged.device_uid,
        credential_generation=1,
        spki_sha256=staged.spki_sha256,
        renewable_until=binding_support.horizon(),
    )
    response, bundle = binding_support.binding_response(
        staged, claims=claims, uri_sans=(binding_support.reference_uri(claims),)
    )
    credential = validate_enrollment_response(
        response,
        staged=staged,
        pinned_trust_bundle_pem=bundle,
        effective_owner=binding_support.OWNER,
    )
    commit_transport_credential(
        tmp_path, staged=staged, credential=credential, pinned_trust_bundle_pem=bundle
    )
    return tmp_path


def test_client_material_is_loaded_through_safe_readers(tmp_path: Path) -> None:
    _committed_home(tmp_path)
    material = load_client_tls_material(tmp_path)
    assert isinstance(material, ClientTlsMaterial)
    assert material.certificate_chain_pem.count(b"-----BEGIN CERTIFICATE-----") >= 2
    assert material.private_key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert material.trust_bundle_pem.startswith(b"-----BEGIN CERTIFICATE-----")
    assert "PRIVATE KEY" not in repr(material)


def test_client_material_rejects_unsafe_key_mode(tmp_path: Path) -> None:
    _committed_home(tmp_path)
    key = tmp_path / "secrets/transport/device-key.pem"
    key.chmod(0o640)
    with pytest.raises(TransportClientError) as caught:
        load_client_tls_material(tmp_path)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


def test_client_material_is_absent_without_a_commit(tmp_path: Path) -> None:
    _staged(tmp_path)
    with pytest.raises(TransportClientError) as caught:
        load_client_tls_material(tmp_path)
    assert caught.value.code == "TRANSPORT_CREDENTIAL_ABSENT"
    assert caught.value.outcome == "NOT_SENT"


def test_material_load_writes_nothing_under_home(tmp_path: Path) -> None:
    _committed_home(tmp_path)
    before = binding_support.inventory(tmp_path)
    assert load_client_tls_material(tmp_path).private_key_pem
    assert binding_support.inventory(tmp_path) == before
