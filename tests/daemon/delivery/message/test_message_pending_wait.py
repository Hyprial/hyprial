"""``message.pending.wait``: the doorbell a Channel child holds instead of polling.

Allen, 2026-10-05: 「请现在去做事件推送的设计和实施」.  The Channel child polled
``message.pending.list`` every 0.5 s; a held wait on the inbox projection lets
a new message end the wait at once and leaves an idle session holding one
request per bound instead of two per second.  The wait only says *when* to
list -- it never drains notices or commits custody -- and it holds an IPC slot,
so it is capacity-bounded and gives up promptly on shutdown.

These run against the real ``DeliveryCustodyFacade`` (not a fake inbox), so
the doorbell under test is the projection every committed mutation passes.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import math
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    InboxMessage,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.application.messaging.inbox_surface import queries
from tests.daemon.delivery.origin.test_inbound_origin import _app  # type: ignore[import-not-found]
from hyprial.daemon.impl.dispatch.admission import LinkAdmission
from tests.support.links.gate import PermissiveLinkGate

READER = "agent:op:node-1:reader"
SESSION = {"from": "reader", "sessionRef": "session-reader"}


class _Events:
    def publish(self, event: object) -> None:
        pass


def _message(message_id: str, *, intent: str = "request") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender="agent:op:node-1:driver",
        recipient=READER,
        payload=json.dumps({"message": message_id}).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )


@pytest.fixture
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    application = _app(tmp_path)
    application._inbox.close()
    path = tmp_path / "custody.sqlite3"
    slot = LinkAdmission()
    application._link_admission = slot
    facade = DeliveryCustodyFacade(
        DeliveryCustodyCoordinator(
            path,
            MemoryDeliveryTransport(),
            _Events(),
            service_options={"link_admission": slot},
        ),
        path,
    )
    application._inbox = facade  # type: ignore[assignment]
    application._inbox_coordinator = facade._coordinator  # type: ignore[attr-defined]
    # driver->reader is agent-to-agent, so the explicit permissive premise
    # keeps this doorbell test about the doorbell (wp4 T13 supplement).
    slot.bind(PermissiveLinkGate())
    # The owned-application teardown closes the inbox and agents.
    return application


def _wait(app: DaemonApplication, known: list[str], hold_ms: int) -> tuple[dict, float]:
    started = time.monotonic()
    result = app.handle(
        "message.pending.wait",
        {**SESSION, "knownMessageIds": known, "holdMs": hold_ms},
    )
    return result, time.monotonic() - started


def _later(seconds: float, action) -> threading.Thread:  # type: ignore[no-untyped-def]
    def run() -> None:
        time.sleep(seconds)
        action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_message_already_unseen_answers_without_holding(app) -> None:  # type: ignore[no-untyped-def]
    app._inbox.receive(_message("m-1"))

    result, elapsed = _wait(app, [], 5_000)

    assert result["changed"] is True and result["held"] is False, result
    assert elapsed < 1.0


def test_a_message_arriving_during_the_hold_ends_it_promptly(app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A long slice separates "woken by the notification" (~0.1 s) from "found
    # by the next slice's own rescan" (~2 s) by more than any runner jitter.
    monkeypatch.setattr(queries, "MESSAGE_WAIT_SLICE_SECONDS", 2.0)
    arrival = _later(0.1, lambda: app._inbox.receive(_message("m-2")))

    result, elapsed = _wait(app, [], 5_000)
    arrival.join()

    assert result["changed"] is True and result["held"] is True, result
    assert 0.08 <= elapsed < 1.0, elapsed


def test_messages_the_caller_already_saw_do_not_end_the_hold(app) -> None:  # type: ignore[no-untyped-def]
    app._inbox.receive(_message("m-3"))

    result, elapsed = _wait(app, ["m-3"], 400)

    assert result["changed"] is False and result["held"] is True, result
    assert elapsed >= 0.35, elapsed


def test_a_new_notice_ends_the_hold_and_is_left_for_the_list(app) -> None:  # type: ignore[no-untyped-def]
    notice = _later(0.2, lambda: app._inbox.receive_system_notice(_message("n-1", intent="system")))

    result, _elapsed = _wait(app, [], 5_000)
    notice.join()

    assert result["changed"] is True, result
    listed = app.handle("message.pending.list", dict(SESSION))
    assert [row["messageId"] for row in listed["messages"]] == ["n-1"]


def test_full_wait_capacity_answers_without_holding(app) -> None:  # type: ignore[no-untyped-def]
    taken = 0
    while app._message_waits.acquire(blocking=False):
        taken += 1
    try:
        result, elapsed = _wait(app, [], 5_000)
    finally:
        for _ in range(taken):
            app._message_waits.release()

    assert taken > 0
    assert result["changed"] is False and result["held"] is False, result
    assert elapsed < 1.0, elapsed


def _close_ipc(app: DaemonApplication) -> None:
    with app._ipc_clients_lock:
        app._ipc_closing = True


@pytest.mark.parametrize(
    "stop",
    [lambda app: app.stop_event.set(), _close_ipc],
    ids=["stop-event", "ipc-closing"],
)
def test_daemon_stop_ends_a_hold_within_one_slice(app, stop) -> None:  # type: ignore[no-untyped-def]
    # _close_ipc_clients sets _ipc_closing before its grace wait, and
    # request_owner.close then allows in-flight IPC 5 s in total.
    stopper = _later(0.2, lambda: stop(app))

    result, elapsed = _wait(app, [], 5_000)
    stopper.join()

    assert result["changed"] is False and result["held"] is True, result
    # Shutdown gives in-flight IPC 5 s; a hold must not use it up.
    assert elapsed < 1.5, elapsed


def test_a_foreign_session_ref_is_refused_before_holding(app) -> None:  # type: ignore[no-untyped-def]
    started = time.monotonic()
    with pytest.raises(DaemonRequestError):
        app.handle(
            "message.pending.wait",
            {"from": "reader", "sessionRef": "not-the-session", "holdMs": 5_000},
        )
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("waiters", [1, 8, 24])
@pytest.mark.parametrize("known_message", [False, True])
def test_idle_waits_do_not_create_projection_changes(
    app, monkeypatch, waiters: int, known_message: bool
) -> None:
    # Other recipients' retained messages make a full projection rebuild real
    # work even though none of these waiters has a new message to consume.
    for index in range(8):
        app._inbox.receive(
            replace(_message(f"other-{index}"), recipient="agent:op:node-1:other")
        )
    known = []
    if known_message:
        app._inbox.receive(_message("already-known"))
        known = ["already-known"]
    coordinator = app._inbox_coordinator
    handler = coordinator._active_handler
    handle = handler._handle
    counts: Counter[str] = Counter()

    def observed(command):
        counts[type(command).__name__] += 1
        return handle(command)

    monkeypatch.setattr(handler, "_handle", observed)
    version = app._inbox.version
    barrier = threading.Barrier(waiters)
    hold_ms = 1_100

    def wait():
        barrier.wait(timeout=5.0)
        return _wait(app, known, hold_ms)[0]

    with ThreadPoolExecutor(max_workers=waiters) as pool:
        results = list(pool.map(lambda _: wait(), range(waiters)))

    assert all(result["held"] and not result["changed"] for result in results)
    max_checks = waiters * (
        1 + math.ceil(hold_ms / 1_000 / queries.MESSAGE_WAIT_SLICE_SECONDS)
    )
    assert app._inbox.version == version, dict(counts)
    assert counts["AdmittedPendingCommand"] <= max_checks, dict(counts)


def test_real_link_window_stays_serial_and_wait_observes_memory_release(
    tmp_path, monkeypatch
) -> None:
    from tests.daemon.links.composition import test_drained_read_back as composition
    from tests.support.links.org import establish

    now = time.time_ns() // 1_000_000
    monkeypatch.setattr(composition, "NOW", now)
    app, facade, harness = composition._composed(tmp_path)
    try:
        establish(harness.fs, READER, composition.DRIVER, now_ms=now)
        harness.tick(now)
        gate = harness.service
        with gate._mem:
            state = gate._state(READER)
            gate._ensure_controller(state, now)
            state.limits = replace(state.limits, w=1)
        assert facade.receive(composition._request("seat-1"), now_ms=now).accepted
        assert facade.receive(composition._request("seat-2"), now_ms=now).accepted
        version = facade.version

        def admitted(operation):
            return tuple(
                message.message_id
                for message in getattr(facade, operation)(READER, now_ms=now)
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            batches = list(pool.map(admitted, (
                "admitted_pending", "admitted_dispatchable", "admitted_pending",
            )))
        assert batches == [("seat-1",)] * 3
        assert facade.version == version

        def release_memory_window() -> None:
            with gate._mem:
                state.limits = replace(state.limits, w=2)

        release = _later(0.1, release_memory_window)
        result, _elapsed = _wait(app, ["seat-1"], 2_000)
        release.join()
        assert result["changed"] and result["held"]
        assert facade.version == version
        assert admitted("admitted_pending") == ("seat-1", "seat-2")
    finally:
        harness.service.close()
