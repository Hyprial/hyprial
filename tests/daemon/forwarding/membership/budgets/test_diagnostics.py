"""Cross-branch control diagnostics retain counts, never payloads or remote prose."""
import json
import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.forwarding.membership import wire
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from hyprial.daemon.impl.ipc.server import _IpcServerMixin
from hyprial.daemon.impl.org.network.ipc import _translate
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService


BASE = {"stage": "control", "cause": "deadline-exceeded", "proxy": "not-configured"}
MAX_RECEIVED = (1 << 20) + 1


def response_error(data, code):
    controller = wire.MembershipWireMixin()
    controller._write_lock = threading.Lock()
    sent = []
    controller._send = sent.append

    def response(match, *, deadline):
        event = {"v": 6, "event": "error", "op": sent[-1]["op"],
                 "requestId": sent[-1]["requestId"], "code": code,
                 "message": "secret-invite-payload", "data": data}
        assert match(event)
        return event

    controller._await_response = response
    with pytest.raises(ForwardingSidecarError) as caught:
        controller.membership_request("invite-redeem", {})
    return caught.value


def domain_data(code):
    return {"operation": "invite-redeem", "leg": "invite-redeem", "budgetSeconds": 65} if code == "MEMBERSHIP_SIDECAR_TIMEOUT" else {}


@pytest.mark.parametrize("code", ["AUTHORITY_UNREACHABLE", "MEMBERSHIP_SIDECAR_TIMEOUT"])
@pytest.mark.parametrize("count", [0, 17, 1 << 20, MAX_RECEIVED])
@pytest.mark.parametrize("cause", ["deadline-exceeded", "transport-failed"])
def test_byte_count_survives_wire_domain_and_ipc(code, count, cause):
    diagnostic = {**BASE, "cause": cause, "bytesReceived": count}
    error = response_error(diagnostic, code)
    assert error.code == code
    expected = diagnostic | domain_data(code)
    assert error.details == expected
    assert "secret-invite-payload" not in str(error)

    def request(op, params):
        raise error

    service = object.__new__(OrgNetworkService)
    service._membership_request = request

    def handle(*args, **kwargs):
        try:
            service._tailnet_call("invite-redeem", {})
        except OrgNetworkError as wrapped:
            raise _translate(wrapped) from wrapped

    owner = SimpleNamespace(_timed_handle=handle)
    result = _IpcServerMixin._response(owner, json.dumps({
        "version": 1, "id": "diagnostic", "method": "org.join", "params": {},
    }).encode())
    assert result["error"]["code"] == code
    assert result["error"]["data"] == expected
    assert "secret-invite-payload" not in json.dumps(result)


@pytest.mark.parametrize("code", ["AUTHORITY_UNREACHABLE", "MEMBERSHIP_SIDECAR_TIMEOUT"])
@pytest.mark.parametrize("data", [None, BASE])
def test_absent_byte_count_is_not_fabricated(code, data):
    error = response_error(data, code)
    assert error.details == (data or {}) | domain_data(code)
    assert "bytesReceived" not in error.details


@pytest.mark.parametrize("value", [-1, MAX_RECEIVED + 1, 1.5, "17", True, False, None, [], {}])
def test_invalid_byte_count_discards_entire_diagnostic(value):
    error = response_error({**BASE, "bytesReceived": value}, "AUTHORITY_UNREACHABLE")
    assert error.details == {}
    assert str(error) == "Tailnet membership command failed."


@pytest.mark.parametrize("stage,cause", [
    ("derp", "deadline-exceeded"), ("sidecar", "transport-failed"),
    ("control", "response-timeout"), ("control", "dns-failed"),
])
def test_count_requires_incomplete_control_response_context(stage, cause):
    error = response_error({**BASE, "stage": stage, "cause": cause, "bytesReceived": 7},
                           "AUTHORITY_UNREACHABLE")
    assert error.details == {}


def test_count_coexists_with_validated_attempts_without_accepting_payload():
    diagnostic = {**BASE, "bytesReceived": 7, "attempts": [
        {"node": "relay.invalid:443", "cause": "tls-failed", "source": "diagnostic-probe"},
    ]}
    assert response_error(diagnostic, "AUTHORITY_UNREACHABLE").details == diagnostic
    for invalid in (
        {**diagnostic, "payload": "secret"},
        {**diagnostic, "attempts": [{"node": "https://secret@relay.invalid:443", "cause": "tls-failed"}]},
        {**diagnostic, "stage": "secret"},
        {**diagnostic, "cause": "secret"},
        {**diagnostic, "proxy": "http://secret@proxy.invalid"},
        {"bytesReceived": 7},
    ):
        assert response_error(invalid, "AUTHORITY_UNREACHABLE").details == {}
