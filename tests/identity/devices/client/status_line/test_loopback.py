"""Real TLS malformed status lines cannot unlock a second mutation ID."""

import pytest

from hyprial.identity.interface import (
    DeviceAuthorityConfig,
    DeviceTransportClient,
    TransportClientError,
)
from tests.identity.devices.binding import support as binding_support
from tests.identity.devices.client import support

from .test_grammar import OPERATION, response

NEXT = "22222222-2222-4222-8222-222222222222"


def invoke(client, staged, operation):
    try:
        data = client.request_certificate(
            operation_id=operation, device_uid=staged.device_uid,
            expected_credential_generation=0, csr_pem=staged.csr_pem,
        )
    except TransportClientError as error:
        return {"code": error.code, "outcome": error.outcome,
                "retryable": error.retryable, "operation_id": error.operation_id}
    return {"success": data == support.certificate_data()}


@pytest.mark.parametrize("line", [b"HTTP/1.1 400 Bad\x00Request", b"HTTP/1.1 400"],
                         ids=["nul-reason", "missing-reason-separator"])
def test_malformed_status_blocks_fresh_id_but_allows_exact_replay(tmp_path, line):
    staged = binding_support.stage(tmp_path)
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [lambda _request: response(line),
         lambda _request: support.raw_response(support.success_envelope(OPERATION, data))],
        max_sessions=2,
    ) as server:
        client = DeviceTransportClient(DeviceAuthorityConfig(
            origin=server.origin, server_name="127.0.0.1", trust_bundle_pem=server.ca.pem,
            connect_timeout=2, handshake_timeout=2, read_timeout=2, write_timeout=2,
        ))
        first = invoke(client, staged, OPERATION)
        fresh = invoke(client, staged, NEXT)
        replay = invoke(client, staged, OPERATION) if fresh.get("code") == "TRANSPORT_OUTCOME_UNRESOLVED" else None
    observations = {
        "first": first, "fresh": fresh, "replay": replay,
        "connections": server.observation.connections,
        "fresh_id_requests": sum(NEXT.encode() in request for request in server.observation.requests),
        "identical_requests": len(server.observation.requests) == 2 and server.observation.requests[0] == server.observation.requests[1],
        "errors": server.observation.errors,
        "thread_stopped": not server._thread.is_alive(),
    }
    assert observations == {
        "first": {"code": "TRANSPORT_RESPONSE_INVALID", "outcome": "UNKNOWN",
                  "retryable": True, "operation_id": OPERATION},
        "fresh": {"code": "TRANSPORT_OUTCOME_UNRESOLVED", "outcome": "UNKNOWN",
                  "retryable": False, "operation_id": OPERATION},
        "replay": {"success": True}, "connections": 2, "fresh_id_requests": 0,
        "identical_requests": True, "errors": [], "thread_stopped": True,
    }
