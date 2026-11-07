"""Waiting on a lifecycle operation must not load the writer it waits on.

Lifecycle root-fix plan S5: ``LifecycleCoordinator.wait`` re-read the journal
every 50 ms, and each read is a round trip through the single state writer --
the queue the operation itself needs to finish.  Production 0.5.0 stack dumps
(2026-10-05) caught the caller inside that read 12 times out of 15.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle import lifecycle_coordinator
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleOperation,
    LifecyclePorts,
    LifecycleState,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission

from ..support import TERMINAL_WAIT_SECONDS, _CompletionPorts, _settles, _spec


def test_a_waiting_caller_reads_the_journal_rarely_and_wakes_on_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A long safety re-check separates "woken by the completion" from "found
    # by the next re-check" by more than any runner jitter.
    monkeypatch.setattr(lifecycle_coordinator, "LIFECYCLE_WAIT_STORE_RECHECK_SECONDS", 3.0)
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="blocked:forward:0")
    coordinator = LifecycleCoordinator(
        tmp_path / "coordinator.sqlite3",
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=0.05,
    )
    reads = 0
    store_result = coordinator._store.result

    def counted(operation_id: str):  # type: ignore[no-untyped-def]
        nonlocal reads
        if threading.current_thread().name == "waiter":
            reads += 1
        return store_result(operation_id)

    coordinator._store.result = counted  # type: ignore[method-assign]
    try:
        assert coordinator.submit(LifecycleOperation.create("blocked", _spec())) is PortAdmission.ACCEPTED
        assert _settles(lambda: len(ports.calls) >= 1)

        outcome: dict[str, object] = {}

        def wait(timeout: float = 1.0) -> None:
            try:
                outcome["result"] = coordinator.wait("blocked", timeout)
            except TimeoutError as error:
                outcome["error"] = error

        waiter = threading.Thread(target=wait, name="waiter")
        waiter.start()
        waiter.join(5)
        assert isinstance(outcome.get("error"), TimeoutError)
        # One read up front and nothing else within the 1 s wait; 50 ms
        # polling made ~20.
        assert reads <= 2, reads

        released_at: list[float] = []
        outcome.clear()
        waiter = threading.Thread(
            target=wait, args=(TERMINAL_WAIT_SECONDS,), name="waiter"
        )
        waiter.start()
        time.sleep(0.1)
        released_at.append(time.monotonic())
        ports.release.set()
        waiter.join(TERMINAL_WAIT_SECONDS)
        woke = time.monotonic() - released_at[0]
        result = outcome.get("result")
        assert result is not None and result.state is LifecycleState.COMPLETED, outcome  # type: ignore[union-attr]
        # Woken by the completion itself, not by the 3 s safety re-check.
        assert woke < 1.5, woke
    finally:
        ports.release.set()
        coordinator.drain(3.0)


def test_an_already_terminal_operation_returns_at_once(tmp_path: Path) -> None:
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router)
    coordinator = LifecycleCoordinator(
        tmp_path / "coordinator.sqlite3",
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=0.05,
    )
    try:
        coordinator.submit(LifecycleOperation.create("done", _spec()))
        assert coordinator.wait("done", TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED
        started = time.monotonic()
        assert coordinator.wait("done", 5.0).state is LifecycleState.COMPLETED
        assert time.monotonic() - started < 0.5
    finally:
        coordinator.drain(3.0)


class _NeverCaches(dict):  # type: ignore[type-arg]
    def __setitem__(self, key, value) -> None:  # type: ignore[no-untyped-def]
        pass


def test_a_terminal_state_written_by_another_path_is_found_by_the_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Independent review on #1138: the safety re-check had no test.  Write
    # the terminal state straight to the journal, bypassing the coordinator's
    # cache and notification; only the re-check can see it.
    monkeypatch.setattr(lifecycle_coordinator, "LIFECYCLE_WAIT_STORE_RECHECK_SECONDS", 0.3)
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="blocked:forward:0")
    coordinator = LifecycleCoordinator(
        tmp_path / "coordinator.sqlite3",
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=0.05,
    )
    # The coordinator's own re-drive would also read the external write and
    # publish it through its cache; keep the cache empty so only the safety
    # re-check can see it.
    coordinator._results = _NeverCaches()  # type: ignore[assignment]
    try:
        coordinator.submit(LifecycleOperation.create("blocked", _spec()))
        assert _settles(lambda: len(ports.calls) >= 1)
        threading.Timer(
            0.1,
            lambda: coordinator._store.set_state("blocked", LifecycleState.FAILED, "external", "X"),
        ).start()
        started = time.monotonic()
        assert coordinator.wait("blocked", 5.0).state is LifecycleState.FAILED
        assert time.monotonic() - started < 0.3 * 1.5 + 0.2
    finally:
        ports.release.set()
        coordinator.drain(3.0)


def test_a_slow_read_is_not_followed_straight_away_by_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Independent review on #1138 (blocking): the re-check interval was
    # measured from before the read, so a read slower than the interval was
    # followed at once by the next one and the waiter always held a read.
    monkeypatch.setattr(lifecycle_coordinator, "LIFECYCLE_WAIT_STORE_RECHECK_SECONDS", 0.2)
    router = CorrelationEventRouter()
    ports = _CompletionPorts(router, block_effect_token="blocked:forward:0")
    coordinator = LifecycleCoordinator(
        tmp_path / "coordinator.sqlite3",
        LifecyclePorts(ports, ports, ports, ports),
        router,
        completion_timeout=0.05,
    )
    reads: list[tuple[float, float]] = []
    store_result = coordinator._store.result

    def slow(operation_id: str):  # type: ignore[no-untyped-def]
        if threading.current_thread().name != "waiter":
            return store_result(operation_id)
        began = time.monotonic()
        time.sleep(0.3)
        reads.append((began, time.monotonic()))
        return store_result(operation_id)

    coordinator._store.result = slow  # type: ignore[method-assign]
    try:
        coordinator.submit(LifecycleOperation.create("blocked", _spec()))
        assert _settles(lambda: len(ports.calls) >= 1)

        def wait() -> None:
            try:
                coordinator.wait("blocked", 1.6)
            except TimeoutError:
                pass

        waiter = threading.Thread(target=wait, name="waiter")
        waiter.start()
        waiter.join(5)
        gaps = [nxt[0] - prev[1] for prev, nxt in zip(reads, reads[1:])]
        assert gaps and min(gaps) >= 0.15, gaps
    finally:
        ports.release.set()
        coordinator.drain(3.0)
