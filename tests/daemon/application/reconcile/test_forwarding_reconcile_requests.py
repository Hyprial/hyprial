"""Reply-before-reconcile bookkeeping for explicit org membership operations.

An ``org.*`` IPC handler must reply once its write is durable and hand the
forwarding reconcile (membership read + slot rebinding through the serial
sidecar I/O owner) to the daemon's ``forwarding-reconcile`` cadence.  These
pin the bookkeeping that attributes a cadence completion back to the request
that asked for it and logs one ``org.<op>.reconcile`` event per request, and
the application glue that creates the cadence on demand and drains it on
close.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.netendpoints.reconcile_requests import (
    ForwardingReconcileRequests,
)
from hyprial.daemon.impl.correlation.bounded_cadence import CadenceCompleted
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict[str, Any]]] = []

    def __call__(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.records.append((level, domain, event, fields))

    def named(self, event: str) -> list[tuple[str, str, str, dict[str, Any]]]:
        return [record for record in self.records if record[2] == event]


def _completion(
    observed_at_ms: int,
    *,
    error: str | None = None,
    code: str | None = None,
    duration_ms: int = 7,
) -> CadenceCompleted[None]:
    return CadenceCompleted(
        epoch=1,
        observed_at_ms=observed_at_ms,
        started_at=0.0,
        duration_ms=duration_ms,
        result=None,
        error=error,
        detail=error,
        code=code,
    )


def test_settle_attributes_requests_made_at_or_before_the_tick() -> None:
    log = _Log()
    requests = ForwardingReconcileRequests(log)

    first = requests.record("org-invite", org="group-acme", user="bob")
    second = requests.record("org-remove", org="group-acme", user="eve")
    assert second >= first

    settled = requests.settle(_completion(second, duration_ms=1200))

    assert [item.reason for item in settled] == ["org-invite", "org-remove"]
    assert requests.pending() == 0
    invite, remove = log.named("org.invite.reconcile")[0], log.named("org.remove.reconcile")[0]
    assert invite[:2] == ("info", "org")
    assert invite[3] == {
        "ok": True, "code": None, "durationMs": 1200, "org": "group-acme", "user": "bob",
    }
    assert remove[3]["user"] == "eve"


def test_a_request_made_after_the_tick_waits_for_the_next_completion() -> None:
    log = _Log()
    clock = [1_000]
    requests = ForwardingReconcileRequests(log, now_ms=lambda: clock[0])
    tick = requests.record("org-invite", org="group-acme", user="bob")
    # A request recorded after the tick's observation cannot have been
    # covered by that tick's reconcile: it must settle with a later one.
    clock[0] += 5
    later = requests.record("org-join", org="group-acme", user="carol")
    assert later == tick + 5

    assert [item.reason for item in requests.settle(_completion(tick))] == ["org-invite"]
    assert requests.pending() == 1
    assert log.named("org.join.reconcile") == []

    assert [item.reason for item in requests.settle(_completion(later))] == ["org-join"]
    assert log.named("org.join.reconcile")[0][3]["ok"] is True


def test_a_failed_reconcile_settles_as_warn_with_the_stable_code() -> None:
    log = _Log()
    requests = ForwardingReconcileRequests(log)
    tick = requests.record("org-invite", org="group-acme", user="bob")

    requests.settle(_completion(
        tick, error="ForwardingSidecarError", code="MEMBERSHIP_SIDECAR_TIMEOUT",
    ))
    (record,) = log.named("org.invite.reconcile")
    assert record[0] == "warn"
    assert record[3]["ok"] is False
    assert record[3]["code"] == "MEMBERSHIP_SIDECAR_TIMEOUT"

    # No code on the exception: the error type is the only stable handle.
    tick = requests.record("org-invite", org="group-acme", user="bob")
    requests.settle(_completion(tick, error="TimeoutError"))
    assert log.named("org.invite.reconcile")[-1][3]["code"] == "TimeoutError"


def test_pending_requests_are_bounded_and_the_overflow_is_loud() -> None:
    log = _Log()
    requests = ForwardingReconcileRequests(log, capacity=2)
    for user in ("a", "b", "c"):
        requests.record("org-invite", org="group-acme", user=user)

    assert requests.pending() == 2
    (overflow,) = log.named("org.reconcile.requests_overflow")
    assert overflow[0] == "warn"
    assert overflow[3]["dropped"]["user"] == "a"


class _Supervisor:
    def close(self) -> None:
        pass


@pytest.fixture
def application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        node_id="reconcile-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        **_deep_module_daemon_dependencies(),
    )
    log = _Log()
    monkeypatch.setattr(app, "_log", log)
    app._forwarding_supervisor = _Supervisor()  # type: ignore[assignment]
    try:
        yield app, log
    finally:
        app._close()


def test_request_forwarding_reconcile_runs_off_the_request_thread_and_settles(
    application, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, log = application
    ran = threading.Event()
    threads: list[str] = []

    def reconcile() -> None:
        threads.append(threading.current_thread().name)
        ran.set()

    monkeypatch.setattr(app, "_reconcile_forwarding_endpoints", reconcile)
    assert app._forwarding_cadence is None

    assert app._request_forwarding_reconcile("org-invite", org="group-acme", user="bob") is True

    assert app._forwarding_cadence is not None, "the request creates the cadence on demand"
    assert ran.wait(2.0)
    assert threads and threads[0] != threading.current_thread().name
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not log.named("org.invite.reconcile"):
        time.sleep(0.01)
    (record,) = log.named("org.invite.reconcile")
    assert record[3]["ok"] is True
    assert record[3]["org"] == "group-acme"

    # The same cadence is what the maintenance tick reuses: no second actor.
    assert app._ensure_forwarding_cadence() is app._forwarding_cadence


def test_request_without_a_supervisor_is_refused_loudly(application) -> None:
    app, log = application
    app._forwarding_supervisor = None

    assert app._request_forwarding_reconcile("org-invite", org="group-acme", user="bob") is False
    assert app._forwarding_cadence is None
    (record,) = log.named("org.reconcile.unscheduled")
    assert record[0] == "warn"
    assert record[3]["reason"] == "org-invite"


def test_access_changed_without_a_supervisor_logs_unscheduled(application) -> None:
    """Through the handler helper, not the inner scheduler (review 0d910d1d2 P2)."""
    from hyprial.daemon.impl.org.network.ipc import _access_changed

    app, log = application
    app._forwarding_supervisor = None
    reply = {"org": "group-acme", "user": "bob"}

    assert _access_changed(app, "org-invite", reply, org="group-acme", user="bob") is reply
    (record,) = log.named("org.reconcile.unscheduled")
    assert record[:2] == ("warn", "org")
    assert record[3] == {"reason": "org-invite", "org": "group-acme", "user": "bob"}
    assert log.named("org.invite.reconcile") == []


def test_a_request_in_the_same_millisecond_as_an_inflight_tick_is_not_settled_by_it() -> None:
    """Review fd203a80d flake: the request settled on the held run's observation.

    A run observed at T may have read state before a request recorded at the
    same millisecond T. Tick observations come from the same monotonic source
    as request times, so a request recorded after a tick was submitted always
    sorts strictly after it, even on a frozen clock.
    """
    log = _Log()
    requests = ForwardingReconcileRequests(log, now_ms=lambda: 1_000)
    inflight = requests.observation()      # maintenance tick submitted at T
    recorded = requests.record("org-invite", org="group-acme", user="bob")  # same ms
    assert recorded > inflight

    assert requests.settle(_completion(inflight)) == ()
    assert requests.pending() == 1
    assert [item.reason for item in requests.settle(_completion(recorded))] == ["org-invite"]
