"""``auth.operation.get`` with ``waitSeconds``: parked WITHOUT the dispatcher lock.

Decision: daemon-first-z5-first-login-tests-20261118.  A parked call answers on
its own: when the daemon reaches serving (across the listener swap), when the
host ends (a definite ``stopped``), or when the seconds run out.  While it is
parked ``ping`` and ``cancel`` are answered, and nothing is left behind: no
driver or connection thread and no descriptor.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from hyprial.kernel import RequestPortError, ipc_errors
from tests.daemon.onboarding.first_login.first_login_support import (
    PROJECTION_THREADS,
    PendingIdentity,
    Parked,
    ask,
    begin_serving,
    leftovers,
    wait_for,
    wait_parked,
)
from tests.daemon.onboarding.wire.wire_support import OWNER

LOGIN = {"intent": "login", "operationKey": "opkey-client-0001"}


def _wait(running, begun, seconds=30):
    return Parked(
        running,
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": seconds},
    )


def test_one_parked_call_returns_serving_after_the_listener_swap(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    parked = _wait(running, begun)
    wait_parked(running)
    assert parked.parked

    released = time.monotonic()
    identity.release()
    assert running.wait() == "promote"
    assert time.monotonic() - released < 4
    assert parked.parked
    assert not running.host.socket_path.exists()

    runtime = running.host.dispatcher.runtime
    runtime._surface = replace(runtime._surface, transport_generation="serving-epoch-1")
    completed = []
    controller = running.host.dispatcher.first_login
    controller.promoted(complete=lambda: completed.append(True))
    answer = parked.wait(5)

    operation = answer["operation"]
    assert operation["state"] == "authenticated"
    assert operation["owner"] == OWNER
    assert operation["completion"]["stage"] == "serving"
    assert operation["completion"]["device"]["ready"] is True
    assert completed == [True]


def test_ping_and_cancel_answer_while_a_wait_is_parked(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    parked = _wait(running, begun)
    wait_parked(running)
    assert parked.parked

    started = time.monotonic()
    assert ask(running, "ping")["phase"] == "bootstrap"
    cancelled = ask(running, "auth.operation.cancel", {"operationId": begun["operationId"]})
    assert time.monotonic() - started < 3

    assert cancelled["cancelled"] is True
    answer = parked.wait(5)
    assert answer["operation"]["state"] == "cancelled"
    assert answer["operation"]["completion"]["stage"] == "stopped"
    assert parked.seconds < 6


def test_the_seconds_running_out_return_the_current_record(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)

    parked = _wait(running, begun, 0.6)
    answer = parked.wait(10)

    assert 0.5 <= parked.seconds < 4
    assert answer["operation"]["state"] == "pending"
    assert answer["operation"]["completion"]["stage"] == "authorizing"
    assert answer["operation"]["verification"]["userCode"] == "WIRE-CODE"
    identity.release()
    assert running.wait() == "promote"


def test_a_stopping_host_answers_a_parked_wait_with_stopped(open_driven):
    before = leftovers()
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    parked = _wait(running, begun)
    wait_parked(running)
    assert parked.parked

    assert running.stop() == "stop"
    answer = parked.wait(5)
    running.host.close()

    assert answer["operation"]["completion"]["stage"] == "stopped"
    assert wait_for(lambda: not leftovers()["threads"], 5)
    assert leftovers()["threads"] == []
    assert wait_for(lambda: leftovers()["fds"] <= before["fds"], 5)
    assert not [t for t in threading.enumerate() if t.name in PROJECTION_THREADS]


def test_a_wait_parked_at_starting_survives_the_listener_close_and_leaks_nothing(
    open_driven,
):
    before = leftovers()
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    parked = _wait(running, begun)
    wait_parked(running)
    identity.release()
    started = time.monotonic()
    assert running.wait() == "promote"
    assert time.monotonic() - started < 3.5
    assert parked.parked

    running.host.dispatcher.first_login.promoted(complete=lambda: None)
    assert parked.wait(5)["operation"]["completion"]["stage"] == "serving"
    running.host.close()

    assert wait_for(lambda: not leftovers()["threads"], 5)
    assert wait_for(lambda: leftovers()["fds"] <= before["fds"], 5)


def test_a_wait_parked_when_promotion_fails_is_answered_stopped(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    parked = _wait(running, begun)
    wait_parked(running)
    identity.release()
    assert running.wait() == "promote"
    assert parked.parked

    running.host.close()
    answer = parked.wait(8)

    assert answer["operation"]["completion"]["stage"] == "stopped"
    assert leftovers()["threads"] == [] or wait_for(lambda: not leftovers()["threads"], 5)


def test_a_login_nobody_drives_answers_at_once_even_with_wait_seconds(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = ask(running, "auth.login.begin", LOGIN)

    started = time.monotonic()
    got = ask(
        running,
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": 20},
    )

    assert time.monotonic() - started < 3
    assert got["operation"]["state"] == "pending"
    assert "completion" not in got["operation"]
    assert len(identity.polls) == 1


def test_wait_seconds_is_bounded_and_an_unknown_operation_is_refused_at_once(open_driven):
    running = open_driven(identity=PendingIdentity())

    with pytest.raises(RequestPortError) as too_long:
        ask(running, "auth.operation.get", {"operationId": "op-0123456789abcdef", "waitSeconds": 901})
    assert too_long.value.code == ipc_errors.INVALID_ARGUMENT

    started = time.monotonic()
    with pytest.raises(RequestPortError) as unknown:
        ask(running, "auth.operation.get", {"operationId": "op-0123456789abcdef", "waitSeconds": 30})
    assert unknown.value.code != ipc_errors.INVALID_ARGUMENT
    assert time.monotonic() - started < 3


def test_a_wait_returns_within_its_seconds_while_the_dispatcher_lock_is_held(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    dispatcher = running.host.dispatcher
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with dispatcher.exclusive():
            holding.set()
            release.wait(timeout=20)

    holder = threading.Thread(target=hold, name="first-login-test-lock-holder", daemon=True)
    holder.start()
    try:
        assert holding.wait(timeout=5)
        parked = _wait(running, begun, 0.5)
        answer = parked.wait(10)
        seconds = parked.seconds
    finally:
        release.set()
        holder.join(timeout=10)

    assert 0.4 <= seconds < 2.0
    assert answer["operation"]["state"] == "pending"
    assert answer["operation"]["completion"]["stage"] == "authorizing"
    assert answer["operation"]["verification"]["userCode"] == "WIRE-CODE"


def test_the_ninth_parked_wait_is_answered_at_once_with_the_current_record(open_driven):
    identity = PendingIdentity()
    running = open_driven(identity=identity)
    begun = begin_serving(running)
    waits = [_wait(running, begun, 30) for _ in range(8)]
    wait_parked(running, 8)
    assert len(running.host._parked) == 8

    started = time.monotonic()
    ninth = ask(
        running,
        "auth.operation.get",
        {"operationId": begun["operationId"], "waitSeconds": 30},
    )

    assert time.monotonic() - started < 3
    assert ninth["operation"]["state"] == "pending"
    assert ninth["operation"]["completion"]["stage"] == "authorizing"
    assert all(item.parked for item in waits)
    ask(running, "auth.operation.cancel", {"operationId": begun["operationId"]})
    for item in waits:
        assert item.wait(10)["operation"]["completion"]["stage"] == "stopped"
    assert wait_for(lambda: len(running.host._parked) == 0, 5)


def test_the_cap_of_parked_waits_is_taken_atomically(open_driven):
    running = open_driven(identity=PendingIdentity())
    host = running.host
    contenders = 24
    barrier = threading.Barrier(contenders)
    release = threading.Event()
    outcomes: list[bool] = []
    guard = threading.Lock()

    def contend() -> None:
        barrier.wait(timeout=10)
        with host._parking() as parked:
            with guard:
                outcomes.append(parked)
            release.wait(timeout=10)

    threads = [threading.Thread(target=contend, daemon=True) for _ in range(contenders)]
    for thread in threads:
        thread.start()
    assert wait_for(lambda: len(outcomes) == contenders, 10)
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert outcomes.count(True) == 8
    assert wait_for(lambda: len(host._parked) == 0, 5)


def test_a_parked_wait_refuses_unknown_params_like_the_normal_path(open_driven):
    running = open_driven(identity=PendingIdentity())
    begun = begin_serving(running)

    started = time.monotonic()
    with pytest.raises(RequestPortError) as refused:
        ask(
            running,
            "auth.operation.get",
            {"operationId": begun["operationId"], "waitSeconds": 30, "unknown": 1},
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert time.monotonic() - started < 3


def test_an_answer_never_pairs_a_pending_view_with_a_stopped_stage():
    from hyprial.daemon.impl.application.onboarding.first_login import FirstLoginRun

    pending = {
        "operationId": "op-0123456789abcdef",
        "kind": "login",
        "state": "pending",
        "owner": None,
        "receiptId": None,
        "verification": None,
    }
    for _ in range(300):
        run = FirstLoginRun(pending)
        seen: list[dict] = []
        stopper = threading.Thread(target=run.stop, daemon=True)
        stopper.start()
        for _ in range(200):
            seen.append(run.answer()["operation"])
        stopper.join(timeout=5)
        for operation in seen:
            assert not (
                operation["state"] == "pending"
                and operation["completion"]["stage"] == "stopped"
            )
