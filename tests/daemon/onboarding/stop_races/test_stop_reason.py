"""The serving application's explicit stop reason (logout versus everything else).

Decision: daemon-first-z2-stop-reason-20261116.  The lifecycle loop reopens
bootstrap only for ``logout``; the reason is the application's own public
policy, not the diagnostic ``_StopRequestTrace`` (which the policy must not
read and which is overwritten by the latest request).
"""

from __future__ import annotations

import signal
import threading

import pytest

from hyprial.daemon.impl.application import DaemonApplication, _StopRequestTrace


@pytest.fixture
def application() -> DaemonApplication:
    app = DaemonApplication.__new__(DaemonApplication)
    app.stop_event = threading.Event()
    app._stop_request_trace = _StopRequestTrace()
    return app


def test_a_run_with_no_recorded_request_is_a_shutdown(application):
    assert application.stop_reason == "shutdown"


def test_a_logout_alone_is_a_logout(application):
    application._request_stop("auth-logout")

    assert application.stop_reason == "logout"
    assert application.stop_event.is_set()


@pytest.mark.parametrize(
    "source", ["signal", "ipc-shutdown", "home-ownership-lost", "restore-failed", "anything-else"]
)
@pytest.mark.parametrize("logout_first", [True, False])
def test_any_other_stop_wins_in_either_order(application, source, logout_first):
    order = ["auth-logout", source] if logout_first else [source, "auth-logout"]
    for item in order:
        application._request_stop(item, signal.SIGTERM if item == "signal" else None)

    assert application.stop_reason == "shutdown"


def test_the_reason_does_not_follow_the_diagnostic_trace(application):
    application._request_stop("signal", signal.SIGTERM)
    application._request_stop("auth-logout")

    assert application._stop_request_trace.last == ("auth-logout", None)
    assert application.stop_reason == "shutdown"


def test_home_ownership_loss_is_recorded_as_a_non_logout_stop(application):
    lost = application.stop_policy.bind(
        application._stop_request_trace.bind(application.stop_event, "home-ownership-lost"),
        "home-ownership-lost",
    )
    application._request_stop("auth-logout")

    lost()

    assert application.stop_reason == "shutdown"
    assert application._stop_request_trace.last == ("home-ownership-lost", None)


def test_the_reason_survives_concurrent_requests(application):
    barrier = threading.Barrier(2)

    def logout():
        barrier.wait()
        for _ in range(200):
            application._request_stop("auth-logout")

    def other():
        barrier.wait()
        application._request_stop("signal", signal.SIGTERM)

    threads = [threading.Thread(target=logout), threading.Thread(target=other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert application.stop_reason == "shutdown"
