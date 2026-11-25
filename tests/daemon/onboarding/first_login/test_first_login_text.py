"""No exception text ever reaches a completion, an answer or a log line.

Decision: daemon-first-z5-first-login-tests-20261118.  A failure is a code plus a
fixed message authored per code.  The exceptions here carry a home path, a fake
token and 5000 characters; none of it may appear in the completion, in any answer
of the daemon, or in the logs the test captures, and every answer must validate
against the declared result schema (an over-long text used to make a successful
login fail result validation over HTTP).
"""

from __future__ import annotations

import json
import logging

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.application.onboarding.provisioning import (
    DeviceProvisioningError,
)
from hyprial.daemon.impl.request_catalog.onboarding.schemas import (
    AUTH_GET_RESULT,
    LOGIN_BEGIN_RESULT,
)
from tests.daemon.onboarding.first_login.first_login_support import (
    BrokenIdentity,
    Parked,
    PendingIdentity,
    ask,
    begin_serving,
    wait_for,
)

SECRET_PATH = "/home/alice/.hyprial/bin/hyprial-tailcat"
SECRET_TOKEN = "tok_live_0123456789abcdef"
LONG = SECRET_PATH + " " + SECRET_TOKEN + " " + "x" * 5000
FORBIDDEN = (SECRET_PATH, SECRET_TOKEN, "xxxxxxxxxx", "/home/alice")


class LeakyDevice:
    def ensure(self, *, home, owner):
        raise DeviceProvisioningError(LONG)


class LeakyIdentity(BrokenIdentity):
    def poll_public(self, operation_id):
        raise RuntimeError(LONG)


def _clean(*documents) -> None:
    text = json.dumps(documents)
    for forbidden in FORBIDDEN:
        assert forbidden not in text


def test_a_device_failure_carries_a_code_and_a_fixed_message_only(open_driven, caplog):
    caplog.set_level(logging.DEBUG)
    running = open_driven(provisioner=LeakyDevice())
    begun = begin_serving(running)
    assert running.wait() == "promote"
    controller = running.host.dispatcher.first_login
    completion = controller.latest().completion()
    controller.promoted(complete=lambda: None)

    error = completion["device"]["error"]
    assert error == {
        "code": "DEVICE_ENSURE_FAILED",
        "message": "the device could not be registered during this login",
    }
    answer = controller.latest().answer()
    _clean(begun, completion, answer)
    assert not [r for r in caplog.records if any(f in r.getMessage() for f in FORBIDDEN)]
    Draft202012Validator(AUTH_GET_RESULT).validate(answer)
    Draft202012Validator(LOGIN_BEGIN_RESULT).validate(begun)


def test_a_failing_worker_reports_a_code_and_the_fixed_generic_message(open_driven, caplog):
    caplog.set_level(logging.DEBUG)
    running = open_driven(identity=LeakyIdentity())
    begun = begin_serving(running)
    run = running.host.dispatcher.first_login.latest()

    assert wait_for(lambda: run.stage == "stopped", 5)

    got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    failure = got["operation"]["completion"]["failure"]
    assert failure == {
        "code": "DAEMON_ERROR",
        "message": "the first login stopped before the daemon was serving",
    }
    _clean(got, run.completion())
    assert not [r for r in caplog.records if any(f in r.getMessage() for f in FORBIDDEN)]
    Draft202012Validator(AUTH_GET_RESULT).validate(got)


def test_a_parked_answer_is_clean_and_schema_valid(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    parked = Parked(
        running,
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": 0.3},
    )

    answer = parked.wait(10)

    _clean(answer)
    Draft202012Validator(AUTH_GET_RESULT).validate(answer)
