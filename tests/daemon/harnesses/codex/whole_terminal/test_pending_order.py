"""E2c: failed snapshot boundaries must precede queued live boundaries."""
from __future__ import annotations

import threading

import pytest

from hyprial.daemon.impl.harnesses.codex.observation import whole_session
from hyprial.daemon.impl.harnesses.codex.observation.whole_session import WholeTerminalTurns

from .support import THREAD_ID
from .test_failure_recovery import wait_until


def boundary(turn_id, phase):
    return {
        "method": f"turn/{phase}",
        "params": {
            "threadId": THREAD_ID,
            "turn": {
                "id": turn_id,
                "status": "inProgress" if phase == "started" else "completed",
            },
        },
    }


def test_failed_adoption_precedes_queued_completion_and_new_start():
    records = []
    calls = 0
    observer = None

    def subscribe():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"turns": [{"id": "A", "status": "inProgress"}]}
        return {"turns": [
            {"id": "A", "status": "completed"},
            {"id": "B", "status": "inProgress"},
        ]}

    failed = False

    def record(*row):
        nonlocal failed
        if not failed:
            failed = True
            observer.notify(boundary("A", "completed"))
            observer.notify(boundary("B", "started"))
            raise RuntimeError("fail first adoption")
        records.append(row)

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID, subscribe=subscribe, read_thread=subscribe,
        record=record, retry_seconds=0.01,
    )
    observer.start()
    try:
        assert wait_until(lambda: calls >= 2 and observer.subscribed)
        assert records == [
            ("A", "started", None), ("A", "ended", "completed"),
            ("B", "started", None),
        ]
        assert observer._pending_report is None
        assert list(observer._open) == ["B"]
    finally:
        observer.stop()


@pytest.mark.parametrize("overflow", [False, True])
def test_persistent_adoption_failure_holds_bounded_backlog(monkeypatch, overflow):
    capacity = 8
    monkeypatch.setattr(whole_session, "_EVENT_QUEUE_CAPACITY", capacity)
    recovered = threading.Event()
    retried = threading.Event()
    records = []
    attempts = []
    logs = []
    calls = 0

    def record(*row):
        attempts.append(row)
        if not recovered.is_set():
            if len(attempts) >= 3:
                retried.set()
            raise RuntimeError("persistent store failure")
        records.append(row)

    def subscribe():
        nonlocal calls
        calls += 1
        return {"turns": [{
            "id": "A", "status": "inProgress" if calls == 1 else "completed",
        }]}

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID, subscribe=subscribe, read_thread=subscribe,
        record=record, retry_seconds=0.01, log=lambda *row: logs.append(row),
    )
    observer._subscribe_once()
    observer.notify(boundary("A", "completed"))
    observer.notify(boundary("B", "started"))
    observer.notify(boundary("B", "completed"))
    observer.notify(boundary("C", "started"))
    observer.notify(boundary("C", "completed"))
    if overflow:
        while not observer._events.full():
            observer.notify(boundary("C", "completed"))
        revision = observer._loss_revision
        for i in range(capacity * 4):
            observer.notify(boundary(f"lost-{i}", "started"))
            observer.notify(boundary(f"lost-{i}", "completed"))
        assert observer._loss_revision == revision + capacity * 8
    queued = observer._events.qsize()
    observer.start()
    try:
        assert retried.wait(2)
        assert observer._worker.is_alive()
        assert not observer.subscribed
        assert records == []
        assert set(attempts) == {("A", "started", None)}
        assert observer._pending_report == ("A", "started", None)
        assert observer._events.qsize() == queued <= capacity
        recovered.set()
        assert wait_until(lambda: observer.subscribed)
        assert records == [
            ("A", "started", None), ("A", "ended", "completed"),
            ("B", "started", None), ("B", "ended", "completed"),
            ("C", "started", None), ("C", "ended", "completed"),
        ]
        assert observer.coverage_gaps >= 1
        assert any(name == "codex.whole-terminal.coverage-gap" for name, _ in logs)
    finally:
        observer.stop()


@pytest.mark.parametrize("phase", ["started", "completed"])
def test_apply_cannot_replace_pending_boundary(phase):
    accepted = []
    failing = False

    def record(*row):
        if failing and row[0] == "A":
            raise RuntimeError("A unavailable; B would succeed")
        accepted.append(row)

    observer = WholeTerminalTurns(
        thread_id=THREAD_ID, subscribe=lambda: {"turns": []},
        read_thread=lambda: {"turns": []}, record=record,
    )
    if phase == "completed":
        assert observer._apply(boundary("A", "started"))
    failing = True
    assert not observer._apply(boundary("A", phase))
    pending = observer._pending_report
    with pytest.raises(RuntimeError, match="pending turn boundary"):
        observer._observe_start("B")
    assert observer._pending_report == pending
    assert not observer._apply(boundary("B", "started"))
    assert observer._pending_report == pending
    failing = False
    assert observer._apply(boundary("B", "started"))
    assert accepted == (
        [("A", "started", None)]
        + ([("A", "ended", "completed")] if phase == "completed" else [])
        + [("B", "started", None)]
    )
