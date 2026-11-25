"""Exact header OWS framing: only SP/HTAB are trimmed, never Unicode space."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.impl.device.client import http as client_http
from hyprial.identity.interface import (
    DeviceAuthorityConfig,
    DeviceTransportClient,
    TransportClientError,
)

from ..binding import support as binding_support
from . import support


def _config(server):
    return DeviceAuthorityConfig(
        origin=server.origin,
        server_name="127.0.0.1",
        trust_bundle_pem=server.ca.pem,
        connect_timeout=2.0,
        handshake_timeout=2.0,
        read_timeout=2.0,
        write_timeout=2.0,
    )


def _response(content_length: bytes, connection: bytes, body: bytes) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + content_length
        + b"\r\nConnection: "
        + connection
        + b"\r\n\r\n"
        + body
    )


LEGAL_OWS = [
    pytest.param(b"LEN", b"close", id="bare"),
    pytest.param(b" LEN", b"close", id="leading-sp"),
    pytest.param(b"LEN ", b"close", id="trailing-sp"),
    pytest.param(b"\tLEN", b"close", id="leading-htab"),
    pytest.param(b"LEN\t", b"close", id="trailing-htab"),
    pytest.param(b" \tLEN\t ", b"close", id="mixed-ows"),
    pytest.param(b"LEN", b"\t close \t", id="connection-ows"),
]

ILLEGAL_PADDING = [
    pytest.param(b"\x0c", b"", id="leading-form-feed"),
    pytest.param(b"", b"\x0c", id="trailing-form-feed"),
    pytest.param(b"\xa0", b"", id="leading-nbsp"),
    pytest.param(b"", b"\xa0", id="trailing-nbsp"),
    pytest.param(b"\x85", b"", id="leading-nel"),
    pytest.param(b"", b"\x85", id="trailing-nel"),
    pytest.param(b"\x0b", b"", id="leading-vertical-tab"),
    pytest.param(b"", b"\x00", id="trailing-nul"),
]


@pytest.mark.parametrize(("prefix", "suffix"), LEGAL_OWS)
def test_space_and_tab_ows_are_accepted(
    tmp_path: Path, prefix, suffix
) -> None:
    staged = binding_support.stage(tmp_path)
    data = support.certificate_data()
    body = support.success_envelope(staged.operation_id, data)
    length = str(len(body)).encode()
    with support.LoopbackAuthority(
        [lambda _request: _response(prefix.replace(b"LEN", length), b"close", body)]
        if prefix != b"LEN"
        else [
            lambda _request: _response(
                length,
                suffix if suffix != b"close" else b"close",
                body,
            )
        ]
    ) as server:
        assert DeviceTransportClient(_config(server)).enroll(staged) == data


@pytest.mark.parametrize(("prefix", "suffix"), ILLEGAL_PADDING)
def test_non_ascii_or_control_padding_is_rejected(
    tmp_path: Path, prefix, suffix
) -> None:
    staged = binding_support.stage(tmp_path)
    body = support.success_envelope(
        staged.operation_id, support.certificate_data()
    )
    length = str(len(body)).encode()
    raw = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + prefix
        + length
        + suffix
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    with support.LoopbackAuthority([lambda _request: raw]) as server:
        client = DeviceTransportClient(_config(server))
        with pytest.raises(TransportClientError) as caught:
            client.enroll(staged)
        assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"
        assert caught.value.outcome == "UNKNOWN"
        with pytest.raises(TransportClientError) as unresolved:
            client.request_certificate(
                operation_id="44444444-4444-4444-8444-444444444444",
                device_uid=staged.device_uid,
                expected_credential_generation=0,
                csr_pem=staged.csr_pem,
            )
        assert unresolved.value.code == "TRANSPORT_OUTCOME_UNRESOLVED"


# The exact six wire counterexamples and seven controls of the independent framing
# review (pa-f7-framing-review-20261022), replayed through the public retire path
# instead of its fake TLS plumbing.  Every illegal Content-Length padding byte is
# a typed TRANSPORT_RESPONSE_INVALID / UNKNOWN that leaves the operation
# unresolved; legal SP/HTAB OWS and the protocol controls keep their outcome.
#
# RED at f7f1a1631, where the Unicode str.strip() removed FF, NBSP and NEL from
# the length value and DeviceTransportClient.retire reported SUCCESS with the
# replay marked resolved.  GREEN from 4592419ac, which trims only SP/HTAB.

RETIRE_OPERATION = "8a71ca3f-2178-4924-8b57-31cb5969e693"
RETIRE_BINDING = "65f7eaa4-c18d-4603-af0d-a58fe9ec50a3"
SECOND_OPERATION = "4d0d6f7e-6f6a-4c9e-9f5f-2a1f5bb1c0d7"
MAX_RESPONSE_BYTES = 65536

INFRA_ILLEGAL_PADDING = [
    pytest.param(b"\x0c", b"", id="form-feed-prefix"),
    pytest.param(b"", b"\x0c", id="form-feed-suffix"),
    pytest.param(b"\xa0", b"", id="nbsp-prefix"),
    pytest.param(b"", b"\xa0", id="nbsp-suffix"),
    pytest.param(b"\x85", b"", id="nel-prefix"),
    pytest.param(b"", b"\x85", id="nel-suffix"),
]

INFRA_LEGAL_OWS = [
    pytest.param(b"", b"", id="bare"),
    pytest.param(b" \t", b"", id="sp-htab-prefix"),
    pytest.param(b"", b" \t", id="sp-htab-suffix"),
]

_ENVELOPE_PREFIX = (
    b'{"ok":true,"version":"'
    + support.ENVELOPE_VERSION.encode()
    + b'","operationId":"'
    + RETIRE_OPERATION.encode()
    + b'","data":'
)

INFRA_INVALID_BODIES = [
    pytest.param(_ENVELOPE_PREFIX + b"[]}", id="shallow-wrong-data-shape"),
    pytest.param(b"{", id="malformed-json"),
    pytest.param(
        _ENVELOPE_PREFIX + b"[" * 20000 + b"0" + b"]" * 20000 + b"}",
        id="deep-array-under-byte-cap",
    ),
    pytest.param(
        _ENVELOPE_PREFIX + b'{"x":' * 9000 + b"0" + b"}" * 9000 + b"}",
        id="deep-object-under-byte-cap",
    ),
]


def _retire_success(operation_id: str) -> dict[str, object]:
    return {
        "retired": True,
        "remoteEnforcementState": "synthetic-local-result",
        "operationId": operation_id,
    }


def _retire_body(operation_id: str) -> bytes:
    return json.dumps(
        {
            "ok": True,
            "version": support.ENVELOPE_VERSION,
            "operationId": operation_id,
            "data": _retire_success(operation_id),
        },
        separators=(",", ":"),
    ).encode()


def _retire_wire(
    operation_id: str = RETIRE_OPERATION,
    *,
    prefix: bytes = b"",
    suffix: bytes = b"",
) -> bytes:
    body = _retire_body(operation_id)
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + prefix
        + str(len(body)).encode()
        + suffix
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )


def _retire(client, operation_id: str) -> dict[str, object]:
    return client.retire(
        operation_id=operation_id,
        binding_id=RETIRE_BINDING,
        expected_credential_generation=1,
    )


def _assert_invalid_unknown_unresolved(client) -> None:
    with pytest.raises(TransportClientError) as caught:
        _retire(client, RETIRE_OPERATION)
    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"
    assert caught.value.outcome == "UNKNOWN"
    # An unaccepted sent response is only settled by an exact replay.
    assert caught.value.retryable is True
    # Unresolved is only observable publicly: a fresh operation on the same
    # route is refused until the ambiguous attempt settles.
    with pytest.raises(TransportClientError) as unresolved:
        _retire(client, SECOND_OPERATION)
    assert unresolved.value.code == "TRANSPORT_OUTCOME_UNRESOLVED"
    assert unresolved.value.outcome == "UNKNOWN"


@pytest.mark.parametrize(("prefix", "suffix"), INFRA_ILLEGAL_PADDING)
def test_infra_length_padding_retire_is_typed_and_unresolved(
    prefix: bytes, suffix: bytes
) -> None:
    wire = _retire_wire(prefix=prefix, suffix=suffix)
    with support.LoopbackAuthority([lambda _request: wire]) as server:
        client = DeviceTransportClient(_config(server))
        _assert_invalid_unknown_unresolved(client)
    assert server.observation.connections == 1
    assert len(server.observation.requests) == 1
    assert server.observation.extra_after_response == 0


@pytest.mark.parametrize(("prefix", "suffix"), INFRA_LEGAL_OWS)
def test_infra_legal_ows_retire_succeeds_and_resolves(
    prefix: bytes, suffix: bytes
) -> None:
    first = _retire_wire(prefix=prefix, suffix=suffix)
    second = _retire_wire(SECOND_OPERATION)
    with support.LoopbackAuthority(
        [lambda _request: first, lambda _request: second], max_sessions=2
    ) as server:
        client = DeviceTransportClient(_config(server))
        assert _retire(client, RETIRE_OPERATION) == _retire_success(
            RETIRE_OPERATION
        )
        # Resolved is only observable publicly: the next operation is accepted.
        assert _retire(client, SECOND_OPERATION) == _retire_success(
            SECOND_OPERATION
        )
    assert server.observation.connections == 2


@pytest.mark.parametrize("body", INFRA_INVALID_BODIES)
def test_infra_protocol_controls_stay_invalid_and_unresolved(
    body: bytes,
) -> None:
    assert 0 < len(body) <= MAX_RESPONSE_BYTES
    wire = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    with support.LoopbackAuthority([lambda _request: wire]) as server:
        client = DeviceTransportClient(_config(server))
        _assert_invalid_unknown_unresolved(client)
    assert server.observation.connections == 1
    assert len(server.observation.requests) == 1


def test_oversized_content_length_is_typed_and_unresolved() -> None:
    """A decimal field longer than the response cap is rejected before int()."""

    wire = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: "
        + b"1" * 5000
        + b"\r\nConnection: close\r\n\r\n"
    )
    with support.LoopbackAuthority([lambda _request: wire]) as server:
        client = DeviceTransportClient(_config(server))
        _assert_invalid_unknown_unresolved(client)
    assert server.observation.connections == 1
    assert len(server.observation.requests) == 1


@pytest.mark.parametrize("failure", [OverflowError, MemoryError])
def test_json_conversion_failures_are_typed_and_unresolved(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """Resource failures at the JSON conversion boundary never escape raw."""

    wire = _retire_wire()

    def fail_json_conversion(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(client_http.json, "loads", fail_json_conversion)
    with support.LoopbackAuthority([lambda _request: wire]) as server:
        client = DeviceTransportClient(_config(server))
        _assert_invalid_unknown_unresolved(client)
    assert server.observation.connections == 1
    assert len(server.observation.requests) == 1


def test_colonless_field_line_is_typed_and_unresolved() -> None:
    """A response field without the mandatory colon is not HTTP framing."""

    body = _retire_body(RETIRE_OPERATION)
    wire = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Broken\r\n"
        b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    with support.LoopbackAuthority([lambda _request: wire]) as server:
        client = DeviceTransportClient(_config(server))
        _assert_invalid_unknown_unresolved(client)
    assert server.observation.connections == 1
    assert len(server.observation.requests) == 1
