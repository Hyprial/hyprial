"""A device that cannot be registered at first login does not fail the login.

Decision: daemon-first-z5-first-login-tests-20261118.  The login succeeds with the
device marked not ready and the initialize receipt left ``promoting`` (recovery
completes it on the promoted start, one attempt per serving start).  The explicit
``onboarding.initialize`` the GUI calls keeps raising ``DEVICE_ENSURE_FAILED``.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.application.onboarding.provisioning import (
    DeviceProvisioningError,
)
from hyprial.kernel import RequestPortError, ipc_errors
from tests.daemon.onboarding.first_login.first_login_support import (
    RecordingDevice,
    UserKeyRecorder,
    ask,
    begin_serving,
)
from tests.daemon.onboarding.wire.wire_support import OWNER


class FailingDevice:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.calls = 0

    def ensure(self, *, home, owner):
        self.calls += 1
        raise DeviceProvisioningError("controlled failure", outcome_unknown=self.unknown)


def _receipts(running, kind):
    runtime = running.host.dispatcher.runtime
    context = runtime.call_context(
        surface=running.host.dispatcher.context.surface, owner=OWNER
    )
    return [record for record in runtime.visible(context) if record.kind == kind]


def test_a_failed_device_still_promotes_and_leaves_initialize_promoting(open_driven):
    keys = UserKeyRecorder()
    running = open_driven(provisioner=FailingDevice(), user_key=keys)

    begin_serving(running)

    assert running.wait() == "promote"
    controller = running.host.dispatcher.first_login
    completion = controller.latest().completion()
    assert completion["stage"] == "starting"
    assert completion["device"]["ready"] is False
    assert completion["device"]["deviceId"] is None
    assert completion["device"]["error"]["code"] == "DEVICE_ENSURE_FAILED"
    assert completion["device"]["retry"] == "serving-start"
    assert completion["device"]["userKey"]["state"] == "unavailable"
    assert completion["failure"] is None
    assert keys.calls == []
    assert [r.state for r in _receipts(running, "initialize")] == ["promoting"]
    assert [r.state for r in _receipts(running, "device")] == ["failed"]

    completed = []
    controller.promoted(complete=lambda: completed.append(True))

    assert completed == []
    after = controller.latest().completion()
    assert after["stage"] == "serving"
    assert after["device"]["ready"] is False


def test_an_unknown_device_outcome_also_promotes_with_the_device_not_ready(open_driven):
    running = open_driven(provisioner=FailingDevice(unknown=True))

    begin_serving(running)

    assert running.wait() == "promote"
    completion = running.host.dispatcher.first_login.latest().completion()
    assert completion["device"]["ready"] is False
    assert completion["device"]["error"]["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert completion["device"]["retry"] == "serving-start"


def test_a_ready_device_completes_initialize_at_the_serving_boundary(open_driven):
    running = open_driven(provisioner=RecordingDevice())
    begin_serving(running)
    assert running.wait() == "promote"
    controller = running.host.dispatcher.first_login

    completed = []
    controller.promoted(complete=lambda: completed.append(True))

    assert completed == [True]
    assert controller.latest().completion()["stage"] == "serving"


def test_an_initialize_without_a_run_completes_as_it_always_did(open_driven):
    running = open_driven(provisioner=RecordingDevice())
    controller = running.host.dispatcher.first_login

    completed = []
    controller.promoted(complete=lambda: completed.append(True))

    assert completed == [True]


def test_the_explicit_initialize_still_refuses_a_device_that_cannot_be_registered(
    open_driven,
):
    device = FailingDevice()
    running = open_driven(provisioner=device)
    begun = ask(
        running, "auth.login.begin", {"intent": "login", "operationKey": "opkey-client-0001"}
    )
    ask(running, "auth.operation.get", {"operationId": begun["operationId"]})

    with pytest.raises(RequestPortError) as refused:
        ask(running, "onboarding.initialize", {"operationKey": "opkey-initialize-0001"})

    assert refused.value.code == "DEVICE_ENSURE_FAILED"
    assert device.calls == 1
    assert running.outcome is None
    assert running.host.dispatcher.first_login.latest() is None


def test_a_user_key_failure_never_strands_a_committed_login(open_driven):
    def broken(*_args):
        raise RuntimeError("controlled key failure")

    running = open_driven(provisioner=RecordingDevice(), user_key=broken)

    begin_serving(running)

    assert running.wait() == "promote"
    completion = running.host.dispatcher.first_login.latest().completion()
    assert completion["device"]["ready"] is True
    assert completion["device"]["userKey"] == {
        "state": "unavailable",
        "code": ipc_errors.USER_KEY_ABSENT,
    }
    assert completion["failure"] is None


def test_a_claimed_run_with_the_device_not_ready_never_completes_initialize(open_driven):
    running = open_driven(provisioner=FailingDevice())
    begin_serving(running)
    assert running.wait() == "promote"
    controller = running.host.dispatcher.first_login
    run = controller.latest()
    assert run.claimed is True
    run.stop()

    completed = []
    controller.promoted(complete=lambda: completed.append(True))

    assert completed == []
    assert [r.state for r in _receipts(running, "initialize")] == ["promoting"]
