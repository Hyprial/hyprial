"""Membership timeout regression tests use virtual sidecar exchange time."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.forwarding.authority import ForwardingControllerAuthority
from hyprial.daemon.impl.forwarding.controller import ForwardingSidecarController
from hyprial.daemon.impl.forwarding.membership import wire
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from hyprial.daemon.impl.org.network.ipc import _translate
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService


def fake_wire(monkeypatch, durations):
    controller = object.__new__(ForwardingSidecarController)
    controller._write_lock = threading.Lock()
    sent = []
    clock = [100.0]
    controller._send = sent.append
    monkeypatch.setattr(wire.time, "monotonic", lambda: clock[0])

    def reply(match, *, deadline):
        request = sent[-1]
        legs = durations[request["op"]] if isinstance(durations, dict) else durations
        for duration in legs:
            clock[0] += duration
            if clock[0] >= deadline:
                raise ForwardingSidecarError("No response")
        response = {"v": 6, "event": "membership-result", "requestId": request["requestId"],
                    "op": request["op"], "data": {"ok": True}}
        assert match(response)
        return response

    controller._await_response = reply
    return controller


@pytest.mark.parametrize("op,durations,budget", [
    ("invite-redeem", [20, 20], 65),
    ("member-announce", [20], 35),
    ("roster-fetch", [20], 35),
])
def test_proxy_exchanges_complete_inside_operation_budget(monkeypatch, op, durations, budget):
    controller = fake_wire(monkeypatch, durations)
    assert controller.membership_request(op, {}, timeout=budget) == {"ok": True}


@pytest.mark.parametrize("op,budget", [("invite-redeem", 65), ("member-announce", 35)])
def test_wire_timeout_reports_operation_leg_and_effective_budget(monkeypatch, op, budget):
    controller = fake_wire(monkeypatch, [budget + 1])
    with pytest.raises(ForwardingSidecarError) as raised:
        controller.membership_request(op, {}, timeout=budget)
    assert raised.value.code == "MEMBERSHIP_SIDECAR_TIMEOUT"
    assert raised.value.details == {"operation": op, "leg": op, "budgetSeconds": budget}
    def failed_request(op, params):
        raise raised.value

    service = object.__new__(OrgNetworkService)
    service._membership_request = failed_request
    with pytest.raises(OrgNetworkError) as wrapped:
        service._tailnet_call(op, {})
    assert _translate(wrapped.value).data == raised.value.details


def test_actor_passes_requested_budget_to_wire():
    calls = []

    class Controller:
        def membership_request(self, op, params, *, timeout=None):
            calls.append(timeout)
            return {}

        def close(self):
            pass

    owner = ForwardingControllerAuthority(Controller())
    try:
        owner.membership_request("invite-redeem", {}, timeout=65)
        assert calls == [pytest.approx(65, abs=0.01)]
    finally:
        owner.close()


def test_expired_queued_command_is_not_sent_to_sidecar(monkeypatch):
    from hyprial.daemon.impl.forwarding import authority

    owner = object.__new__(ForwardingControllerAuthority)
    owner._guard = threading.Lock()
    reply = SimpleNamespace(ready=SimpleNamespace(set=lambda: None))
    owner._pending = {"request": reply}
    owner._controller = SimpleNamespace(membership_request=lambda *args, **kwargs: pytest.fail("stale command sent"))
    monkeypatch.setattr(authority.time, "monotonic", lambda: 100)
    owner._receive(authority._MembershipSidecar("request", "invite-redeem", {}, 65, 99))
    assert reply.error.code == "MEMBERSHIP_SIDECAR_TIMEOUT"
    assert reply.error.details == {"operation": "invite-redeem", "leg": "sidecar-queue", "budgetSeconds": 65}
    assert owner._pending == {}


@pytest.mark.parametrize("actor", [False, True])
def test_ipc_actor_wire_complete_three_proxy_exchanges(monkeypatch, tmp_path, actor):
    from hyprial.daemon.impl.org.network.ipc import _request_service

    controller = fake_wire(monkeypatch, {"invite-redeem": [20, 20], "member-announce": [20]})
    controller.close = lambda: None
    owner = ForwardingControllerAuthority(controller) if actor else controller
    app = SimpleNamespace(
        hyprial_home=tmp_path, node_id="node", owner="owner",
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=object()),
        _start_forwarding_supervisor=lambda: None,
        _forwarding_supervisor=owner,
    )
    try:
        service = _request_service(app)
        assert service._membership_request("invite-redeem", {}) == {"ok": True}
        assert service._membership_request("member-announce", {}) == {"ok": True}
    finally:
        owner.close()
