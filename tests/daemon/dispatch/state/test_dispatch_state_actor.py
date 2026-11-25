from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from hyprial.kernel import ActorCell, ActorSpec, AdmissionResult

from hyprial.daemon.impl.dispatch.runtime.state import (
    DispatchOwnedMap,
    DispatchOwnedSet,
    DispatchStateAuthority,
    DispatchStateOperation,
    DispatchTable,
)


@dataclass(frozen=True)
class Attempt:
    delivery_id: str
    generation: int


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for the close race")
        time.sleep(0.001)


def test_close_drains_command_already_accepted(actor_host, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = [0]
    real_receive = DispatchStateAuthority._receive

    def receive(self, command):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(4)
        return real_receive(self, command)

    monkeypatch.setattr(DispatchStateAuthority, "_receive", receive)
    owner = DispatchStateAuthority(actor_host=actor_host)
    drain_started = threading.Event()
    real_drain = ActorCell.drain

    def observed_drain(cell, timeout=5.0):
        if cell is owner._cell:
            drain_started.set()
        return real_drain(cell, timeout)

    monkeypatch.setattr(ActorCell, "drain", observed_drain)
    results: dict[str, str] = {}

    def call(label: str) -> None:
        try:
            owner.call(
                DispatchTable.INFLIGHT,
                DispatchStateOperation.PUT,
                label,
                label,
                timeout=1.0,
            )
            results[label] = "ok"
        except Exception as caught:
            results[label] = type(caught).__name__

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert entered.wait(3)
    second = threading.Thread(target=call, args=("second",))
    second.start()
    _wait(lambda: len(owner._pending) == 2)
    outcome: dict[str, bool] = {}

    def close() -> None:
        outcome["closed"] = owner.close(2.0)

    closer = threading.Thread(target=close)
    closer.start()
    _wait(drain_started.is_set)
    release.set()
    for thread in (first, second, closer):
        thread.join(4)
        assert not thread.is_alive()

    assert results == {"first": "ok", "second": "ok"}
    assert owner._pending == {}
    assert outcome == {"closed": True}


def test_close_timeout_keeps_accepted_commands(actor_host, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = [0]
    real_receive = DispatchStateAuthority._receive

    def receive(self, command):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(4)
        return real_receive(self, command)

    monkeypatch.setattr(DispatchStateAuthority, "_receive", receive)
    owner = DispatchStateAuthority(actor_host=actor_host)
    results: dict[str, str] = {}

    def call(label: str) -> None:
        try:
            owner.call(
                DispatchTable.INFLIGHT,
                DispatchStateOperation.PUT,
                label,
                label,
                timeout=1.0,
            )
            results[label] = "ok"
        except Exception as caught:
            results[label] = type(caught).__name__

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert entered.wait(3)
    second = threading.Thread(target=call, args=("second",))
    second.start()
    _wait(lambda: len(owner._pending) == 2)
    assert owner.close(0.2) is False
    assert len(owner._pending) == 2
    release.set()
    for thread in (first, second):
        thread.join(4)
        assert not thread.is_alive()
    _wait(lambda: owner._pending == {})
    assert results == {"first": "ok", "second": "ok"}
    assert owner.close(2.0) is True


def test_attempt_result_hold_and_notice_tables_are_one_mailbox(actor_host) -> None:
    with pytest.raises(ValueError, match="actor_host is required"):
        DispatchStateAuthority()
    peer = actor_host.spawn(ActorSpec("borrowed-host-peer", lambda: lambda _command: None))
    owner = DispatchStateAuthority(actor_host=actor_host)
    attempts = DispatchOwnedMap[Attempt](owner, DispatchTable.INFLIGHT)
    notices = DispatchOwnedSet(owner, DispatchTable.NOTICE_FAILURES_LOGGED)
    attempts["message-1"] = Attempt("message-1", 1)
    assert attempts.get("message-1") == Attempt("message-1", 1)
    assert attempts.values() == (Attempt("message-1", 1),)
    notices.add("message-1")
    assert "message-1" in notices
    notices.discard("message-1")
    assert "message-1" not in notices
    assert attempts.pop("message-1") == Attempt("message-1", 1)
    assert attempts.values() == ()
    projection = owner.projection()
    assert dict(projection.table_sizes)[DispatchTable.INFLIGHT.value] == 0
    assert projection.version >= 4
    assert owner.close(2)
    assert peer.tell("still-live") is AdmissionResult.ACCEPTED
    assert peer.stop(2)
