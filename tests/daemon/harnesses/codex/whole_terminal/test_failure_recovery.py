"""E2 review R1-R3 regressions: test-owned transport and report sink failures."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.harnesses.codex.carrier import CodexInteractiveCarrier
from hyprial.daemon.impl.harnesses.codex.observation.whole_session import WholeTerminalTurns

from .support import THREAD_ID, FakeAppServer, carrier_app_server


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def event(phase, status):
    return {
        "method": f"turn/{phase}",
        "params": {
            "threadId": THREAD_ID,
            "turn": {"id": "typed", "status": status},
        },
    }


def test_socket_loss_reconnect_reconciles_before_restoring_coverage(tmp_path, socket_root):
    server = FakeAppServer(socket_root / "native.sock")
    server.start()
    app = carrier_app_server(server.socket_path, cwd=tmp_path)
    reconnect_allowed = threading.Event()
    first = True
    records = []
    logs = []

    def subscribe():
        nonlocal first
        if not first and not reconnect_allowed.is_set():
            raise ConnectionError("test holds reconnect")
        first = False
        return app.subscribe_thread()

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID, subscribe=subscribe,
        read_thread=app.read_thread_snapshot,
        record=lambda *row: records.append(row),
        log=lambda *row: logs.append(row), retry_seconds=0.01,
    )
    remove = app.add_notification_listener(observer.notify)
    observer.start()
    try:
        assert wait_until(lambda: observer.subscribed)
        server.begin_tui_turn("before-loss")
        assert wait_until(lambda: ("before-loss", "started", None) in records)
        old = app._socket
        old.close()
        assert wait_until(lambda: not observer.subscribed)
        assert observer._worker.is_alive()
        assert wait_until(lambda: not server.clients)
        server.end_turn("before-loss")
        reconnect_allowed.set()
        assert wait_until(lambda: observer.subscribed)
        assert app._socket is not old
        assert ("before-loss", "ended", "completed") in records
        assert server.methods().count("thread/resume") >= 2
        assert "initialize" in server.methods()
        server.begin_tui_turn("after-loss")
        server.end_turn("after-loss")
        assert wait_until(lambda: ("after-loss", "ended", "completed") in records)
        assert any(name == "codex.whole-terminal.coverage-gap" for name, _ in logs)
    finally:
        remove()
        observer.stop()
        app.stop()
        server.stop()


def test_initial_adoption_failure_withdraws_coverage_and_retries():
    attempted = threading.Event()
    recovered = threading.Event()
    records = []

    def record(*row):
        attempted.set()
        if not recovered.is_set():
            raise RuntimeError("temporary report-store failure")
        records.append(row)

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID,
        subscribe=lambda: {
            "id": THREAD_ID,
            "turns": [{"id": "already-open", "status": "inProgress"}],
        },
        read_thread=lambda: {"id": THREAD_ID, "turns": []},
        record=record, retry_seconds=0.01,
    )
    observer.start()
    try:
        assert attempted.wait(2)
        observer._worker.join(timeout=0.1)
        assert observer._worker.is_alive()
        assert not observer.subscribed
        recovered.set()
        assert wait_until(lambda: observer.subscribed)
        assert records == [("already-open", "started", None)]
    finally:
        observer.stop()


@pytest.mark.parametrize("phase", ["started", "completed"])
def test_failed_sink_does_not_commit_dedup_or_open_state(phase):
    records = []
    failing = False

    def record(*row):
        if failing:
            raise RuntimeError("temporary report-store failure")
        records.append(row)

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID, subscribe=lambda: {"turns": []},
        read_thread=lambda: {"turns": []}, record=record,
    )
    if phase == "completed":
        observer._apply(event("started", "inProgress"))
    failing = True
    message = event(phase, "inProgress" if phase == "started" else "completed")
    observer._apply(message)
    assert ("typed" in observer._open) == (phase == "completed")
    failing = False
    observer._apply(message)
    observer._apply(message)
    expected = [("typed", "started", None)]
    if phase == "completed":
        expected.append(("typed", "ended", "completed"))
    assert records == expected


def test_failed_live_boundary_is_redriven_without_duplicate_notification():
    attempts = threading.Event()
    recovered = threading.Event()
    records = []

    def record(*row):
        attempts.set()
        if not recovered.is_set():
            raise RuntimeError("temporary report-store failure")
        records.append(row)

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID, subscribe=lambda: {"turns": []},
        read_thread=lambda: {"turns": []}, record=record, retry_seconds=0.01,
    )
    observer.start()
    try:
        assert wait_until(lambda: observer.subscribed)
        observer.notify(event("started", "inProgress"))
        assert attempts.wait(2)
        assert wait_until(lambda: not observer.subscribed)
        observer.notify(event("completed", "completed"))
        recovered.set()
        assert wait_until(lambda: ("typed", "ended", "completed") in records)
        assert records == [("typed", "started", None), ("typed", "ended", "completed")]
        assert wait_until(lambda: observer.subscribed)
    finally:
        observer.stop()


def test_loss_during_adoption_cannot_restore_stale_coverage():
    observer = WholeTerminalTurns(
        thread_id=THREAD_ID,
        subscribe=lambda: {"turns": [{"id": "typed", "status": "inProgress"}]},
        read_thread=lambda: {"turns": []},
        record=lambda *_: observer.notify({"method": "_hyprial/connection-lost"}),
    )
    observer._subscribe_once()
    assert not observer.subscribed


def test_adoption_failure_is_redriven_even_if_turn_ends_before_retry():
    records = []
    failing = True
    snapshot = {"turns": [{"id": "typed", "status": "inProgress"}]}

    def record(*row):
        if failing:
            raise RuntimeError("temporary report-store failure")
        records.append(row)

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID, subscribe=lambda: snapshot,
        read_thread=lambda: snapshot, record=record,
    )
    observer._subscribe_once()
    assert not observer.subscribed
    failing = False
    snapshot = {"turns": [{"id": "typed", "status": "completed"}]}
    observer._subscribe_once()
    assert observer.subscribed
    assert records == [("typed", "started", None), ("typed", "ended", "completed")]


def test_coverage_publishes_gap_even_after_fast_reconnect_and_retries_failed_refresh():
    carrier = object.__new__(CodexInteractiveCarrier)
    carrier.actor = "test-actor"
    carrier.session_ref = THREAD_ID
    carrier._whole_terminal = SimpleNamespace(subscribed=True, coverage_gaps=1)
    carrier._declared_turn_coverage = "whole-session"
    carrier._declared_coverage_gaps = 0
    records = []
    failing = True

    def request(method, params):
        if failing:
            raise ConnectionError("daemon unavailable")
        records.append((method, params["turnCoverage"]))
        return {"ok": True}

    carrier.daemon_request = request
    with pytest.raises(ConnectionError):
        carrier._sync_turn_coverage()
    assert carrier._declared_turn_coverage == "whole-session"
    assert carrier._declared_coverage_gaps == 0
    failing = False
    carrier._sync_turn_coverage()
    carrier._sync_turn_coverage()
    assert records == [
        ("session.refresh", "carrier-deliveries"),
        ("session.refresh", "whole-session"),
    ]
