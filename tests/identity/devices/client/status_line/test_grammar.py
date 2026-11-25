"""RFC 9112 section 4 byte grammar precedes authority envelope acceptance."""

import pytest

from hyprial.identity.impl.device.client import http
from hyprial.identity.interface import TransportClientError
from tests.identity.devices.client import support

OPERATION = "11111111-1111-4111-8111-111111111111"
ILLEGAL = [value for value in range(32) if value != 9] + [127]


def response(line):
    wire = support.raw_response(support.error_envelope(OPERATION, "CSR_INVALID", False))
    return line + wire[wire.index(b"\r\n"):]


def decode(line):
    try:
        http._decode_response(response(line), operation_id=OPERATION,
                              route=http.DEVICE_CERTIFICATE_ROUTE)
    except TransportClientError as error:
        return error.code, error.outcome, error.retryable, error.operation_id
    return "unexpected-success"


@pytest.mark.parametrize("value", ILLEGAL, ids=lambda value: f"0x{value:02x}")
def test_reason_control_bytes_are_invalid_unknown(value):
    result = decode(b"HTTP/1.1 400 Bad" + bytes([value]) + b"Request")
    assert result == ("TRANSPORT_RESPONSE_INVALID", "UNKNOWN", True, OPERATION)


def test_status_requires_space_even_when_reason_is_absent():
    assert decode(b"HTTP/1.1 400") == (
        "TRANSPORT_RESPONSE_INVALID", "UNKNOWN", True, OPERATION
    )


@pytest.mark.parametrize("reason", [
    pytest.param(b"Bad Request", id="ascii"),
    pytest.param(b"Bad\tRequest", id="htab"),
    pytest.param(b"", id="empty-with-separator"),
    pytest.param(b"\t" + bytes(range(32, 127)) + bytes(range(128, 256)),
                 id="all-legal-reason-octets"),
])
def test_legal_reason_octets_remain_canonical_rejection(reason):
    assert decode(b"HTTP/1.1 400 " + reason) == (
        "CSR_INVALID", "REJECTED", False, OPERATION
    )


@pytest.mark.parametrize("line", [
    b"HTTP/1.0 400 Bad Request", b"HTTP/1.1\t400 Bad Request",
    b"HTTP/1.1  400 Bad Request", b"HTTP/1.1 40 Bad Request",
    b"HTTP/1.1 0400 Bad Request", b"HTTP/1.1 100 Continue",
])
def test_existing_version_status_and_separator_constraints_remain(line):
    assert decode(line) == ("TRANSPORT_RESPONSE_INVALID", "UNKNOWN", True, OPERATION)
