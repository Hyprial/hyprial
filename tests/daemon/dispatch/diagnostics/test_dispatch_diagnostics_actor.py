from __future__ import annotations

import threading
import time

from hyprial.kernel import ActorCell

from hyprial.daemon.impl.dispatch.runtime.diagnostics import DispatchDiagnostics


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
    real_receive = DispatchDiagnostics._receive

    def receive(self, command):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(4)
        return real_receive(self, command)

    monkeypatch.setattr(DispatchDiagnostics, "_receive", receive)
    owner = DispatchDiagnostics(actor_host=actor_host)
    drain_started = threading.Event()
    real_drain = ActorCell.drain

    def observed_drain(cell, timeout=5.0):
        if cell is owner._cell:
            drain_started.set()
        return real_drain(cell, timeout)

    monkeypatch.setattr(ActorCell, "drain", observed_drain)
    results: dict[str, str] = {}

    def classify(label: str) -> None:
        try:
            owner.classify(True, timeout=1.0)
            results[label] = "ok"
        except Exception as caught:
            results[label] = type(caught).__name__

    first = threading.Thread(target=classify, args=("first",))
    first.start()
    assert entered.wait(3)
    second = threading.Thread(target=classify, args=("second",))
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
    real_receive = DispatchDiagnostics._receive

    def receive(self, command):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(4)
        return real_receive(self, command)

    monkeypatch.setattr(DispatchDiagnostics, "_receive", receive)
    owner = DispatchDiagnostics(actor_host=actor_host)
    results: dict[str, str] = {}

    def classify(label: str) -> None:
        try:
            owner.classify(True, timeout=1.0)
            results[label] = "ok"
        except Exception as caught:
            results[label] = type(caught).__name__

    first = threading.Thread(target=classify, args=("first",))
    first.start()
    assert entered.wait(3)
    second = threading.Thread(target=classify, args=("second",))
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


def test_conversation_opener_and_counters_have_one_owner(actor_host) -> None:
    owner = DispatchDiagnostics(actor_host=actor_host)
    assert owner.open_conversation("thread-a", "send-1")
    assert owner.open_conversation("thread-a", "send-1")
    assert not owner.open_conversation("thread-a", "send-2")
    owner.classify(True)
    owner.classify(False)
    projection = owner.projection()
    assert projection.without_pac == 1
    assert projection.conversation == 1
    assert projection.known_conversations == 1
    assert owner.close(2)
