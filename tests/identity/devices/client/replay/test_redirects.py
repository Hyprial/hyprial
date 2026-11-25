"""A redirect cannot certify that an already sent mutation did not commit."""

from pathlib import Path

import pytest

from hyprial.identity.interface import TransportClientError
from tests.identity.devices.binding import support as binding_support
from tests.identity.devices.client import support

from .test_operations import (
    FIRST_OPERATION,
    NEXT_OPERATION,
    _certificate,
    _client,
)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("body", [b"not-an-envelope", b"{}"])
def test_redirect_stays_unknown_and_cannot_unlock_a_new_operation(
    tmp_path: Path, status: int, body: bytes
):
    staged = binding_support.stage(tmp_path)
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [
            lambda _request: support.raw_response(
                body,
                status=f"{status} Redirect",
                extra_headers=(("Location", "https://untrusted.invalid/"),),
            ),
            lambda _request: support.raw_response(
                support.success_envelope(FIRST_OPERATION, data)
            ),
        ],
        max_sessions=2,
    ) as server:
        client = _client(server)
        with pytest.raises(TransportClientError) as redirected:
            _certificate(client, staged, FIRST_OPERATION)
        error = redirected.value
        assert error.code == "TRANSPORT_REDIRECT_REFUSED"
        assert error.http_status == status
        assert error.operation_id == FIRST_OPERATION
        assert error.retryable is False
        assert error.outcome == "UNKNOWN"
        with pytest.raises(TransportClientError) as blocked:
            _certificate(client, staged, NEXT_OPERATION)
        assert blocked.value.code == "TRANSPORT_OUTCOME_UNRESOLVED"
        assert blocked.value.outcome == "UNKNOWN"
        assert blocked.value.operation_id == FIRST_OPERATION
        assert _certificate(client, staged, FIRST_OPERATION) == data
    assert server.observation.connections == 2
    assert server.observation.requests[0] == server.observation.requests[1]
    assert server.observation.errors == []
