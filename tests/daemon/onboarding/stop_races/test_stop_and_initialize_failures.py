"""``shutdown`` racing an accepted initialize, and failures after durable acceptance.

Decision: daemon-first-z2-stop-and-failure-20261116 (design 6.3 items 2 and 3).
Every case drives the real wire host over its real socket in an isolated
ownerless home; races are made deterministic by placing the competing event at
an exact seam of the host instead of sleeping.
"""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.application.onboarding.bootstrap import wire as wire_module
from hyprial.daemon.impl.onboarding.operations import OperationService
from hyprial.kernel import RequestPortError, daemon_request_once, ipc_errors

from tests.daemon.onboarding.wire.wire_support import OWNER, assert_home_released


def _ask(running, method, params=None):
    return daemon_request_once(
        method, params, socket_path=running.host.socket_path, timeout=10.0
    )


def _authenticate(running, key):
    begun = _ask(running, "auth.login.begin", {"intent": "login", "operationKey": key})
    got = _ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "authenticated"


def _claim(running):
    runtime = running.host.dispatcher.runtime
    return runtime, runtime.claimed_operation_id, runtime.call_context(
        surface=runtime._surface, owner=OWNER
    )


def _claim_state_after_close(ownerless, runtime, claim_id, context):
    return OperationService.open(ownerless.state / "onboarding").get(context, claim_id).state


def test_a_stop_acknowledged_during_the_promotion_drain_wins_and_the_claim_ends_unknown(
    open_wire, ownerless, monkeypatch
):
    running = open_wire()
    host = running.host
    seen: list[str] = []
    original = host._close_listener

    def stop_arrives_after_promote_was_decided() -> None:
        seen.append(host.outcome or "undecided")
        host.request_stop()
        original()

    monkeypatch.setattr(host, "_close_listener", stop_arrives_after_promote_was_decided)
    _authenticate(running, "race-login-0000001")
    accepted = _ask(running, "onboarding.initialize", {"operationKey": "race-init-00000001"})
    assert accepted["state"] == "promoting"

    assert running.wait() == wire_module.STOP
    assert seen == [wire_module.PROMOTE]
    runtime, claim_id, context = _claim(running)
    assert claim_id is not None
    record = json.loads((ownerless.state / "bootstrap.json").read_text())
    assert record["phase"] == "promoting"

    host.close()

    assert _claim_state_after_close(ownerless, runtime, claim_id, context) == "unknown"
    assert_home_released(ownerless)
    assert not ownerless.socket.exists()


def test_initialize_is_refused_once_a_stop_was_acknowledged(open_wire, ownerless):
    running = open_wire()
    _authenticate(running, "race-login-0000002")
    host = running.host
    host.request_stop()

    reply = host._respond(
        json.dumps(
            {"version": 1, "id": "late", "method": "onboarding.initialize",
             "params": {"operationKey": "race-init-00000002"}}
        ).encode()
    )

    document = json.loads(reply.frame)
    assert document["error"]["code"] == ipc_errors.DAEMON_UNAVAILABLE
    assert reply.after is None and reply.unacknowledged is None
    assert host.dispatcher.runtime.claimed_operation_id is None
    assert host.dispatcher.runtime.phase == "authenticated"
    assert running.wait() == wire_module.STOP


def test_a_stop_during_a_logout_reopen_still_exits(open_wire, ownerless, monkeypatch):
    running = open_wire()
    host = running.host
    original = host._close_listener

    def stop_arrives_after_reopen_was_decided() -> None:
        host.request_stop()
        original()

    monkeypatch.setattr(host, "_close_listener", stop_arrives_after_reopen_was_decided)
    _authenticate(running, "race-login-0000003")
    reply = _ask(running, "auth.logout", {"operationKey": "race-logout-0000001", "confirm": True})
    assert reply["loggedOut"] is True

    assert running.wait() == wire_module.STOP


@pytest.mark.parametrize("failure", ["encode", "registry"])
def test_a_failure_after_durable_acceptance_never_leaves_the_host_serving(
    open_wire, ownerless, monkeypatch, failure
):
    running = open_wire()
    host = running.host
    _authenticate(running, "race-login-0000004")
    if failure == "encode":
        original_encode = host._encode

        def encode(document):
            if document.get("result", {}).get("state") == "promoting":
                raise ValueError("injected encode failure")
            return original_encode(document)

        monkeypatch.setattr(host, "_encode", encode)
    else:
        original_call = host.dispatcher.call

        def call(method, params=None):
            result = original_call(method, params)
            if method == "onboarding.initialize":
                raise RuntimeError("injected registry failure")
            return result

        monkeypatch.setattr(host.dispatcher, "call", call)

    with pytest.raises(RequestPortError) as refused:
        _ask(running, "onboarding.initialize", {"operationKey": "race-init-00000003"})
    assert refused.value.code == ipc_errors.DAEMON_ERROR

    assert running.wait() == wire_module.UNACKNOWLEDGED
    assert host.dispatcher.take_post_reply() is None
    assert not ownerless.socket.exists()
    runtime, claim_id, context = _claim(running)
    assert claim_id is not None
    assert json.loads((ownerless.state / "bootstrap.json").read_text())["phase"] == "promoting"

    host.close()

    assert _claim_state_after_close(ownerless, runtime, claim_id, context) == "unknown"
    assert_home_released(ownerless)


def test_a_failure_before_acceptance_keeps_serving(open_wire):
    running = open_wire()
    with pytest.raises(RequestPortError) as refused:
        _ask(running, "onboarding.initialize", {"operationKey": "race-init-00000004"})
    assert refused.value.code == "IDENTITY_UNBOUND"

    assert _ask(running, "ping")["phase"] == "bootstrap"
    assert running.host.outcome is None
    assert running.stop() == wire_module.STOP
