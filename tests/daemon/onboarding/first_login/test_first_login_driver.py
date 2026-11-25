"""One ``auth.login.begin`` with ``until: serving`` makes the daemon drive the login.

Decision: daemon-first-z5-first-login-tests-20261118.  The wire host, dispatcher,
gateway, runtime and journal are the shipped code in an isolated ownerless home;
the identity worker, device provisioner and user-key step are controlled ports.
The client never calls ``auth.operation.get`` in the first test: the daemon polls
the issuer, registers the device, ensures the user key, accepts initialize and
hands the process to promotion by itself.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from hyprial.daemon.impl.onboarding.operations import journal
from hyprial.kernel import DaemonRequestError, RequestPortError, ipc_errors
from tests.daemon.onboarding.first_login.first_login_support import (
    DRIVER_THREAD,
    WIRE_THREAD,
    CommitOnCancelIdentity,
    BrokenIdentity,
    ExpiredIdentity,
    Parked,
    PendingIdentity,
    RecordingDevice,
    UserKeyRecorder,
    ask,
    begin_serving,
    wait_for,
    wait_parked,
)
from tests.daemon.onboarding.wire.wire_support import OWNER

LOGIN = {"intent": "login", "operationKey": "opkey-client-0001"}


def test_until_serving_promotes_with_no_client_poll(open_driven):
    keys = UserKeyRecorder()
    device = RecordingDevice()
    running = open_driven(provisioner=device, user_key=keys)

    begun = begin_serving(running)

    assert begun["state"] == "pending"
    assert begun["completion"]["stage"] == "authorizing"
    assert begun["verification"]["userCode"] == "WIRE-CODE"
    assert running.wait() == "promote"
    controller = running.host.dispatcher.first_login
    run = controller.latest()
    completion = run.completion()
    assert completion["stage"] == "starting"
    assert completion["identity"]["mode"] == "casdoor"
    assert completion["identity"]["issuer"] == "https://issuer.example.test"
    assert completion["device"]["ready"] is True
    assert completion["device"]["deviceId"] == "controlled-device"
    assert completion["device"]["keyGeneration"] == 2
    assert completion["device"]["userKey"] == {
        "state": "unbound",
        "code": "USER_KEY_UNBOUND",
    }
    assert completion["device"]["retry"] is None
    assert device.calls == 1
    assert len(keys.calls) == 1
    home, state_dir, owner, record = keys.calls[0]
    assert (owner, record.device_id) == (OWNER, "controlled-device")
    assert state_dir == running.host.dispatcher.runtime.state_dir
    runtime = running.host.dispatcher.runtime
    assert runtime.phase == "promoting"
    assert runtime.claimed_operation_id is not None
    assert controller.join(0.01) is True


def test_the_driver_is_the_only_poller_of_a_login_it_drives(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)

    for _ in range(3):
        got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
        assert got["operation"]["state"] == "pending"
        assert got["operation"]["completion"]["stage"] == "authorizing"
        assert got["operation"]["verification"]["userCode"] == "WIRE-CODE"

    assert identity.polls
    assert set(identity.polls) == {DRIVER_THREAD}
    identity.release()
    assert running.wait() == "promote"
    assert set(identity.polls) == {DRIVER_THREAD}


def test_a_second_client_with_another_key_attaches_to_the_same_login(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    first = begin_serving(running, "opkey-first-0001")

    second = begin_serving(running, "opkey-second-0002")

    assert second["operationId"] == first["operationId"]
    assert second["verification"]["userCode"] == first["verification"]["userCode"]
    assert second["verification"]["verificationUri"] == first["verification"]["verificationUri"]
    assert second["completion"]["stage"] == "authorizing"
    controller = running.host.dispatcher.first_login
    assert sum(1 for t in threading.enumerate() if t.name == DRIVER_THREAD) == 1
    assert controller.run_for(first["operationId"]) is controller.latest()
    identity.release()
    assert running.wait() == "promote"


def test_a_client_clocked_login_in_flight_still_answers_in_progress(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    ask(running, "auth.login.begin", LOGIN)

    for key in (LOGIN["operationKey"], "opkey-other-0002"):
        with pytest.raises(RequestPortError) as refused:
            begin_serving(running, key)
        assert refused.value.code == ipc_errors.OPERATION_IN_PROGRESS

    assert running.host.dispatcher.first_login.latest() is None
    assert not [t for t in threading.enumerate() if t.name == DRIVER_THREAD]


def test_a_driven_login_in_flight_refuses_a_client_clocked_begin_with_another_key(
    open_driven,
):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begin_serving(running)

    with pytest.raises(RequestPortError) as refused:
        ask(running, "auth.login.begin", {**LOGIN, "operationKey": "opkey-gui-000009"})

    assert refused.value.code == ipc_errors.OPERATION_IN_PROGRESS


def test_cancel_stops_the_driver_and_returns_to_awaiting_auth(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    controller = running.host.dispatcher.first_login
    run = controller.latest()

    cancelled = ask(running, "auth.operation.cancel", {"operationId": begun["operationId"]})

    assert cancelled["cancelled"] is True and cancelled["state"] == "cancelled"
    assert wait_for(lambda: run.stage == "stopped", 5)
    assert controller.join(5) is True
    assert running.host.dispatcher.runtime.phase == "awaiting-auth"
    got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "cancelled"
    assert got["operation"]["completion"]["stage"] == "stopped"
    polls_after = len(identity.polls)
    time.sleep(1.3)
    assert len(identity.polls) == polls_after
    again = begin_serving(running, "opkey-again-0003")
    assert again["operationId"] != begun["operationId"]
    identity.release()
    assert running.wait() == "promote"


def test_cancel_after_the_identity_committed_answers_as_today_and_the_daemon_finishes(
    open_driven,
):
    identity = CommitOnCancelIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)

    cancelled = ask(running, "auth.operation.cancel", {"operationId": begun["operationId"]})

    assert cancelled["cancelled"] is False
    assert cancelled["reason"] == "identity-committed"
    assert running.wait() == "promote"
    assert running.host.dispatcher.first_login.latest().stage == "starting"


def test_an_expired_authorization_ends_the_driver_in_awaiting_auth(open_driven):
    identity = ExpiredIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    controller = running.host.dispatcher.first_login
    run = controller.latest()

    assert wait_for(lambda: run.stage == "stopped", 5)
    assert controller.join(5) is True

    got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "expired"
    assert got["operation"]["completion"]["stage"] == "stopped"
    assert running.host.dispatcher.runtime.phase == "awaiting-auth"
    retry = begin_serving(running, "opkey-retry-0004")
    assert retry["operationId"] != begun["operationId"]
    assert retry["completion"]["stage"] == "authorizing"


def test_a_worker_that_cannot_be_polled_ends_the_run_and_releases_the_login(open_driven):
    identity = BrokenIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    controller = running.host.dispatcher.first_login
    run = controller.latest()

    assert wait_for(lambda: run.stage == "stopped", 5)
    assert controller.join(5) is True

    completion = run.completion()
    assert completion["failure"] == {
        "code": ipc_errors.DAEMON_ERROR,
        "message": "the first login stopped before the daemon was serving",
    }
    assert running.host.dispatcher.runtime.phase == "awaiting-auth"
    got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "cancelled"
    assert got["operation"]["completion"]["stage"] == "stopped"
    assert identity.cancels == 1
    assert len(identity.polls) == 1


def test_an_authenticated_bootstrap_nobody_drives_refuses_until_serving(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = ask(running, "auth.login.begin", LOGIN)
    identity.release()
    ask(running, "auth.operation.get", {"operationId": begun["operationId"]})

    for key in (LOGIN["operationKey"], "opkey-other-0002"):
        with pytest.raises(RequestPortError) as refused:
            begin_serving(running, key)
        assert refused.value.code == ipc_errors.IDENTITY_CONFLICT
    assert running.host.dispatcher.first_login.latest() is None


def test_a_stopping_host_ends_the_driver_and_the_login_stays_unclaimed(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    controller = running.host.dispatcher.first_login
    run = controller.latest()

    assert running.stop() == "stop"
    running.host.close()

    assert run.stage == "stopped"
    assert controller.join(0.01) is True
    assert begun["completion"]["stage"] == "authorizing"
    assert not [t for t in threading.enumerate() if t.name == DRIVER_THREAD]


def test_a_login_without_until_is_exactly_what_the_gui_clocks_today(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity, plain=True)
    begun = ask(running, "auth.login.begin", LOGIN)

    assert set(begun) == {"ok", "operationId", "intent", "state", "verification", "owner"}
    for polls in (1, 2, 3):
        got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
        assert set(got["operation"]) == {
            "operationId",
            "kind",
            "state",
            "owner",
            "receiptId",
            "verification",
        }
        assert got["operation"]["state"] == "pending"
        assert len(identity.polls) == polls
    assert set(identity.polls) == {WIRE_THREAD}
    assert ask(running, "auth.login.begin", LOGIN) == begun
    identity.release()
    got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "authenticated"
    assert "completion" not in got["operation"]
    initialized = ask(running, "onboarding.initialize", {"operationKey": "opkey-initialize-0001"})
    assert initialized["state"] == "promoting"
    assert running.wait() == "promote"
    assert not [t for t in threading.enumerate() if t.name == DRIVER_THREAD]


def test_an_explicit_until_authenticated_is_the_default_behaviour(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)

    begun = ask(running, "auth.login.begin", {**LOGIN, "until": "authenticated"})

    assert set(begun) == {"ok", "operationId", "intent", "state", "verification", "owner"}
    assert ask(running, "auth.login.begin", LOGIN) == begun
    assert running.host.dispatcher.first_login.latest() is None
    assert identity.polls == []


def test_a_serving_or_owner_env_daemon_refuses_until_serving_before_the_port():
    from hyprial.daemon.impl.request_catalog.onboarding.handlers import auth_login_begin

    class OwnedApp:
        @property
        def onboarding(self):
            raise AssertionError("the port must not be acquired")

        def onboarding_context(self, *, trusted_message_origin):
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "no frame")

    params = {"intent": "login", "operationKey": "opkey-owned-0001", "until": "serving"}
    with pytest.raises(DaemonRequestError) as refused:
        auth_login_begin(OwnedApp(), params, trusted_message_origin="ipc")
    assert refused.value.code == ipc_errors.IDENTITY_CONFLICT


def test_the_completion_never_carries_a_device_code_or_a_token(open_driven, caplog):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    seen = [json.dumps(begun["completion"])]
    got = ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    seen.append(json.dumps(got["operation"]["completion"]))
    identity.release()
    assert running.wait() == "promote"
    seen.append(json.dumps(running.host.dispatcher.first_login.latest().completion()))

    for text in seen:
        for forbidden in ("WIRE-CODE", "issuer.example.test/device", "token", "secret"):
            assert forbidden not in text
    assert "WIRE-CODE" not in caplog.text


def test_promote_is_signalled_only_after_the_driver_left_the_dispatcher_lock(open_driven):
    running = open_driven()
    dispatcher = running.host.dispatcher
    host = running.host
    held_when_signalled: list[bool] = []
    real_finish = host._finish

    def watching(outcome):
        if outcome == "promote":
            held_when_signalled.append(dispatcher._lock._is_owned())
        real_finish(outcome)

    host._finish = watching
    begin_serving(running)

    assert running.wait() == "promote"
    assert held_when_signalled == [False]
    runtime = dispatcher.runtime
    assert runtime.phase == "promoting"
    assert dispatcher.first_login.join(5) is True


def test_an_accept_that_fails_after_the_claim_is_not_promoted_and_ends_unacknowledged(
    open_driven,
):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    runtime = running.host.dispatcher.runtime
    real_accept = runtime.accept_initialize

    def failing(**options):
        real_accept(**options)
        raise RuntimeError("controlled failure after the claim")

    runtime.accept_initialize = failing
    begun = begin_serving(running)
    controller = running.host.dispatcher.first_login
    run = controller.latest()
    parked = Parked(
        running,
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": 30},
    )
    wait_parked(running)
    identity.release()

    assert running.wait() == "initialize-unacknowledged"
    assert wait_for(lambda: run.stage == "stopped", 5)
    assert run.completion()["failure"] == {
        "code": ipc_errors.DAEMON_ERROR,
        "message": "the first login stopped before the daemon was serving",
    }
    assert parked.wait(10)["operation"]["completion"]["stage"] == "stopped"
    assert runtime._post_reply is None
    assert runtime.phase == "promoting"
    assert controller.join(5) is True
    running.host.close()
    records = journal.read_journal(runtime._operations.root).snapshot.records
    assert [r.state for r in records if r.kind == "initialize"] == ["unknown"]


def test_a_gui_initialize_that_wins_leaves_the_driven_run_serving_not_stopped(open_driven):
    running = open_driven(provisioner=RecordingDevice())
    controller = running.host.dispatcher.first_login
    real = controller._start_serving

    def gui_first(driven):
        ask(running, "onboarding.initialize", {"operationKey": "opkey-gui-initialize-01"})
        real(driven)

    controller._start_serving = gui_first
    begin_serving(running)

    assert running.wait() == "promote"
    run = controller.latest()
    assert controller.join(5) is True
    assert run.stage == "starting" and not run.claimed
    assert run.completion()["failure"] is None
    completed: list[bool] = []
    controller.promoted(complete=lambda: completed.append(True))
    assert completed == [True]
    assert run.stage == "serving"
    assert run.completion()["failure"] is None
