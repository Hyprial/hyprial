"""A device that cannot be registered at first login: the login still succeeds.

Decision: daemon-first-z5-first-login-process-20261118.  The process is the same,
the login ends serving, and ``onboarding.status`` says honestly that the device is
not ready.  Retry is exactly the existing rule: one recovery attempt per serving
start, and that attempt now also runs on a start that promoted a bootstrap.
"""

from __future__ import annotations

import signal
import time

from tests.integration.daemon_lifecycle.test_same_pid_lifecycle import OWNER

from .conftest import pytestmark  # noqa: F401
from .test_one_operation import _first_login


def _recovery_outcome(lifecycle, child, budget=60):
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        for item in lifecycle.records():
            if item["event"] == "recovery-finished":
                return item["outcome"]
        assert child.poll() is None, lifecycle.child_log(child)
        time.sleep(0.1)
    raise AssertionError("the promoted start never ran its recovery pass")


def _stop(lifecycle, child):
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=90) == 0, lifecycle.child_log(child)
    lifecycle.assert_nothing_left(child.pid)


def test_a_failed_device_serves_not_ready_and_recovery_fails_the_same_way(lifecycle):
    child = lifecycle.spawn(Z5_BOOTSTRAP_DEVICE="fail", Z3_SERVING_DEVICE="fail")

    first, _begun, operation = _first_login(lifecycle, child)

    completion = operation["completion"]
    assert completion["stage"] == "serving"
    assert operation["state"] == "authenticated" and operation["owner"] == OWNER
    assert completion["device"]["ready"] is False
    assert completion["device"]["error"]["code"] == "DEVICE_ENSURE_FAILED"
    assert completion["device"]["retry"] == "serving-start"
    ping = lifecycle.serving(child)
    assert ping["pid"] == child.pid
    assert _recovery_outcome(lifecycle, child) == "device-failed"
    time.sleep(3)
    events = [item["event"] for item in lifecycle.records()]
    assert events.count("device-ensure-failed") == 2
    assert events.count("recovery-finished") == 1
    status = lifecycle.call("onboarding.status")
    assert status["authenticated"] is True
    assert (status["deviceReady"], status["initialized"]) == (False, False)
    assert status["nextActions"] == ["device.ensure"]
    assert lifecycle.ping()["epoch"] != first["epoch"]
    _stop(lifecycle, child)


def test_the_promoted_start_registers_the_device_by_itself_and_completes_initialize(
    lifecycle,
):
    child = lifecycle.spawn(Z5_BOOTSTRAP_DEVICE="fail")

    _first, _begun, operation = _first_login(lifecycle, child)

    assert operation["completion"]["device"]["ready"] is False
    assert _recovery_outcome(lifecycle, child) == "resumed"
    status = lifecycle.call("onboarding.status")
    assert (status["deviceReady"], status["initialized"]) == (True, True)
    assert status["nextActions"] == []
    assert lifecycle.ping()["pid"] == child.pid
    _stop(lifecycle, child)
