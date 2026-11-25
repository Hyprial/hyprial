"""Wire witnesses separating definite rejection from unresolved mutation."""

from pathlib import Path

import pytest

from hyprial.identity.interface import (
    DeviceAuthorityConfig,
    DeviceTransportClient,
    TransportClientError,
)
from tests.identity.devices.binding import support as binding_support
from tests.identity.devices.client import support

FIRST_OPERATION = "11111111-1111-4111-8111-111111111111"
NEXT_OPERATION = "22222222-2222-4222-8222-222222222222"
BINDING = "33333333-3333-4333-8333-333333333333"


def _client(server) -> DeviceTransportClient:
    return DeviceTransportClient(
        DeviceAuthorityConfig(
            origin=server.origin,
            server_name="127.0.0.1",
            trust_bundle_pem=server.ca.pem,
            connect_timeout=2,
            handshake_timeout=2,
            read_timeout=2,
            write_timeout=2,
        )
    )


def _certificate(client, staged, operation_id, *, generation=0):
    return client.request_certificate(
        operation_id=operation_id,
        device_uid=staged.device_uid,
        expected_credential_generation=generation,
        csr_pem=staged.csr_pem,
    )


def _reject(operation_id=FIRST_OPERATION):
    return support.raw_response(
        support.error_envelope(operation_id, "CSR_INVALID", False),
        status="400 Bad Request",
    )


def _assert_rejection(error):
    assert error.code == "CSR_INVALID"
    assert error.http_status == 400
    assert error.retryable is False
    assert error.outcome == "REJECTED"
    assert error.operation_id == FIRST_OPERATION


@pytest.mark.parametrize("route", ["certificate", "retire"])
def test_definite_rejection_allows_a_new_operation(tmp_path: Path, route: str):
    staged = binding_support.stage(tmp_path)
    data = support.certificate_data() if route == "certificate" else {
        "retired": True,
        "remoteEnforcementState": "pending",
        "operationId": NEXT_OPERATION,
    }

    def invoke(client, operation_id):
        if route == "certificate":
            return _certificate(client, staged, operation_id)
        return client.retire(
            operation_id=operation_id,
            binding_id=BINDING,
            expected_credential_generation=1,
        )

    with support.LoopbackAuthority(
        [
            lambda _request: _reject(),
            lambda _request: support.raw_response(
                support.success_envelope(NEXT_OPERATION, data)
            ),
        ],
        max_sessions=2,
    ) as server:
        client = _client(server)
        with pytest.raises(TransportClientError) as caught:
            invoke(client, FIRST_OPERATION)
        _assert_rejection(caught.value)
        assert invoke(client, NEXT_OPERATION) == data
    assert server.observation.connections == 2
    assert len(server.observation.requests) == 2
    assert FIRST_OPERATION.encode() in server.observation.requests[0]
    assert NEXT_OPERATION.encode() in server.observation.requests[1]
    assert server.observation.extra_after_response == 0
    assert server.observation.errors == []


def test_rejected_operation_allows_exact_replay(tmp_path: Path):
    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority(
        [lambda _request: _reject(), lambda _request: _reject()], max_sessions=2
    ) as server:
        client = _client(server)
        for _ in range(2):
            with pytest.raises(TransportClientError) as caught:
                _certificate(client, staged, FIRST_OPERATION)
            _assert_rejection(caught.value)
    assert len(server.observation.requests) == 2
    assert server.observation.requests[0] == server.observation.requests[1]
    assert server.observation.errors == []


@pytest.mark.parametrize("outcome", ["UNKNOWN", "REJECTED"])
@pytest.mark.parametrize("change", ["body", "route"])
def test_same_id_rejects_body_or_route_changes_before_connect(
    tmp_path: Path, outcome: str, change: str
):
    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority(
        [lambda _request: None if outcome == "UNKNOWN" else _reject()],
        max_sessions=2,
    ) as server:
        client = _client(server)
        with pytest.raises(TransportClientError) as first:
            _certificate(client, staged, FIRST_OPERATION)
        assert first.value.outcome == outcome
        with pytest.raises(TransportClientError) as changed:
            if change == "body":
                _certificate(client, staged, FIRST_OPERATION, generation=1)
            else:
                client.retire(
                    operation_id=FIRST_OPERATION,
                    binding_id=BINDING,
                    expected_credential_generation=1,
                )
        assert changed.value.code == "TRANSPORT_REPLAY_MISMATCH"
        assert changed.value.outcome == "NOT_SENT"
        assert changed.value.retryable is False
    assert server.observation.connections == 1
    assert len(server.observation.requests) == 1
    assert server.observation.errors == []


@pytest.mark.parametrize("unknown", ["lost-response", "authority-unavailable"])
def test_unknown_blocks_new_operation_until_exact_replay_is_rejected(
    tmp_path: Path, unknown: str
):
    staged = binding_support.stage(tmp_path)
    data = support.certificate_data()
    unavailable = support.raw_response(
        support.error_envelope(FIRST_OPERATION, "AUTHORITY_UNAVAILABLE", True),
        status="503 Service Unavailable",
    )
    with support.LoopbackAuthority(
        [
            lambda _request: None if unknown == "lost-response" else unavailable,
            lambda _request: _reject(),
            lambda _request: support.raw_response(
                support.success_envelope(NEXT_OPERATION, data)
            ),
        ],
        max_sessions=3,
    ) as server:
        client = _client(server)
        with pytest.raises(TransportClientError) as first:
            _certificate(client, staged, FIRST_OPERATION)
        assert first.value.outcome == "UNKNOWN"
        with pytest.raises(TransportClientError) as blocked:
            _certificate(client, staged, NEXT_OPERATION)
        assert blocked.value.code == "TRANSPORT_OUTCOME_UNRESOLVED"
        assert blocked.value.outcome == "UNKNOWN"
        assert blocked.value.operation_id == FIRST_OPERATION
        with pytest.raises(TransportClientError) as replay:
            _certificate(client, staged, FIRST_OPERATION)
        _assert_rejection(replay.value)
        assert _certificate(client, staged, NEXT_OPERATION) == data
    assert server.observation.connections == 3
    assert len(server.observation.requests) == 3
    assert server.observation.requests[0] == server.observation.requests[1]
    assert NEXT_OPERATION.encode() in server.observation.requests[2]
    assert server.observation.errors == []
