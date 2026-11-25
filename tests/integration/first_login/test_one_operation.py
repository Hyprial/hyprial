"""A first login is ONE daemon operation, on the unmodified ``hyprial daemon run``.

Decision: daemon-first-z5-first-login-process-20261118.  The client begins with
``until: serving`` and then makes exactly one ``auth.operation.get`` with
``waitSeconds``; everything between (the issuer poll, the device, the user key,
initialize, the listener swap and the serving start) is the daemon's.  The
identity port, issuer profile and device provisioner are the only controlled
parts (``daemon_lifecycle/child_entry.py``); real OIDC and a real Tailcat
sidecar are not exercised and nothing here claims they are.  Every process the
test starts is reaped by the fixture, also on failure.
"""

from __future__ import annotations

import signal

from tests.integration.daemon_lifecycle.test_same_pid_lifecycle import OWNER

from .conftest import pytestmark  # noqa: F401

WAIT_SECONDS = 60
CLIENT_SECONDS = 75


def _first_login(lifecycle, child):
    first = lifecycle.wait_phase({"bootstrap"}, child)
    assert first["pid"] == child.pid and first["owner"] is None
    calls = ["ping"]
    begun = lifecycle.call(
        "auth.login.begin",
        {"intent": "login", "operationKey": "z5-first-login-key-0001", "until": "serving"},
    )
    calls.append("auth.login.begin")
    assert begun["completion"]["stage"] == "authorizing"
    assert begun["verification"]["userCode"] == "Z2-CODE"
    done = lifecycle.call(
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": WAIT_SECONDS},
        timeout=CLIENT_SECONDS,
    )
    calls.append("auth.operation.get")
    assert calls == ["ping", "auth.login.begin", "auth.operation.get"]
    return first, begun, done["operation"]


def test_begin_then_one_wait_ends_in_serving_in_the_same_process(lifecycle):
    child = lifecycle.spawn()

    first, begun, operation = _first_login(lifecycle, child)

    completion = operation["completion"]
    assert operation["operationId"] == begun["operationId"]
    assert operation["state"] == "authenticated" and operation["owner"] == OWNER
    assert completion["stage"] == "serving"
    assert completion["device"]["ready"] is True
    assert completion["device"]["deviceId"] == "z2-node-device"
    assert completion["device"]["retry"] is None
    assert completion["identity"]["mode"] == "casdoor"
    ping = lifecycle.serving(child)
    assert ping["pid"] == child.pid and ping["owner"] == OWNER
    assert ping["epoch"] != first["epoch"]
    status = lifecycle.call("onboarding.status")
    assert status["phase"] == "serving" and status["authenticated"] is True
    assert (status["deviceReady"], status["initialized"]) == (True, True)
    assert status["nextActions"] == []
    assert child.poll() is None, lifecycle.child_log(child)

    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)


def test_a_cancelled_wait_leaves_the_daemon_in_bootstrap_and_ready_for_the_next_login(
    lifecycle,
):
    child = lifecycle.spawn(Z5_BOOTSTRAP_HOLD="1")
    first = lifecycle.wait_phase({"bootstrap"}, child)
    begun = lifecycle.call(
        "auth.login.begin",
        {"intent": "login", "operationKey": "z5-first-login-key-0001", "until": "serving"},
    )
    cancelled = lifecycle.call("auth.operation.cancel", {"operationId": begun["operationId"]})

    assert cancelled["cancelled"] is True
    ping = lifecycle.wait_phase({"bootstrap"}, child)
    assert ping["pid"] == child.pid and ping["epoch"] == first["epoch"]
    assert lifecycle.call("onboarding.status")["authenticated"] is False
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)
