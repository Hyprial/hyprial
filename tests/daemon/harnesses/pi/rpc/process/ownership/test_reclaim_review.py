from __future__ import annotations

import asyncio
import queue
import threading

import pytest

from hyprial.daemon.impl.harnesses.streaming import StreamingTurnProcess
from hyprial.daemon.impl.harnesses.streaming.protocol import _OBSERVER_STOP, _STOP
from hyprial.kernel import AdmissionResult


class _HeldEnterClient:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    async def __aenter__(self) -> _HeldEnterClient:
        self._entered.set()
        assert await asyncio.to_thread(self._release.wait, 5.0)
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):  # type: ignore[no-untyped-def]
        if False:
            yield None

    async def interrupt(self) -> None:
        return None


def _held_process(
    thread_name: str,
    *,
    observer=None,  # type: ignore[no-untyped-def]
) -> tuple[StreamingTurnProcess, threading.Event]:
    entered = threading.Event()
    release = threading.Event()
    process = StreamingTurnProcess(
        harness="pi",
        label="review failed-stop pi RPC",
        client_factory=lambda: _HeldEnterClient(entered, release),
        thread_name=thread_name,
        stop_timeout_seconds=0.01,
        force_stop=lambda: None,
        force_stopped=lambda: False,
        force_stop_join_seconds=0.01,
        on_turn_failure=observer,
    )
    assert entered.wait(timeout=2.0)
    return process, release


def _cleanup(process: StreamingTurnProcess, release: threading.Event) -> None:
    process._stopping.set()
    try:
        process._io_requests.put_nowait(_STOP)
    except queue.Full:
        pass
    release.set()
    relay = process._turn_runtime._relay._thread
    if relay.is_alive():
        process._turn_runtime.drain(2.0)
    process._thread.join(timeout=2.0)
    observer = process._observer_thread
    if observer is not None and observer.is_alive():
        with process._observer_admission_lock:
            process._observer_stop_requested.set()
            assert process._observer_queue is not None
            try:
                process._observer_queue.put_nowait(_OBSERVER_STOP)
            except queue.Full:
                pass
        observer.join(timeout=2.0)


def test_overloaded_observer_exits_after_refused_sentinel_and_owner_recovery() -> None:
    observed = threading.Event()
    recovered = threading.Event()

    def observer(_failure: str) -> AdmissionResult:
        observed.set()
        if not recovered.is_set():
            return AdmissionResult.OVERLOADED
        return AdmissionResult.ACCEPTED

    process, release = _held_process("refused-observer-sentinel", observer=observer)
    observer_thread = process._observer_thread
    observer_queue = process._observer_queue
    assert observer_thread is not None and observer_queue is not None
    try:
        process._dispatch_turn_failure("held observation")
        assert observed.wait(timeout=2.0)
        while not observer_queue.full():
            process._dispatch_turn_failure("queued observation")

        with pytest.raises(RuntimeError, match="failure observer did not drain"):
            process._reclaim_owned_runtime()

        recovered.set()
        observer_thread.join(timeout=2.0)
        assert not observer_thread.is_alive()
    finally:
        recovered.set()
        _cleanup(process, release)


def test_observer_drain_failure_still_cancels_timers_and_releases_waiters() -> None:
    observed = threading.Event()
    recovered = threading.Event()

    def observer(_failure: str) -> AdmissionResult:
        observed.set()
        if not recovered.is_set():
            return AdmissionResult.OVERLOADED
        return AdmissionResult.ACCEPTED

    process, release = _held_process("independent-reclaim-steps", observer=observer)
    timer = threading.Timer(30.0, lambda: None)
    timer.start()
    failure_done = threading.Event()
    failure_outcome: list[tuple[bool, int]] = []
    interrupt_done = threading.Event()
    interrupt_outcome: list[bool] = []
    process._abort_timers[1] = timer
    process._aborted_generations.add(1)
    process._abort_callbacks[1] = lambda: None
    process._failure_waiters[(1, "delivery")] = (failure_done, failure_outcome)
    process._interrupt_waiters["interrupt"] = (
        interrupt_done,
        interrupt_outcome,
    )
    try:
        process._dispatch_turn_failure("held observation")
        assert observed.wait(timeout=2.0)

        with pytest.raises(RuntimeError, match="failure observer did not drain"):
            process._reclaim_owned_runtime()

        assert timer.finished.is_set()
        assert process._abort_timers == {}
        assert process._aborted_generations == set()
        assert process._abort_callbacks == {}
        assert failure_done.is_set() and failure_outcome == [(False, 0)]
        assert interrupt_done.is_set() and interrupt_outcome == [False]
    finally:
        recovered.set()
        timer.cancel()
        timer.join(timeout=2.0)
        _cleanup(process, release)


def test_false_turn_runtime_drain_is_a_reclaim_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, release = _held_process("false-runtime-drain")
    original_drain = process._turn_runtime.drain
    monkeypatch.setattr(process._turn_runtime, "drain", lambda _timeout: False)
    try:
        with pytest.raises(RuntimeError, match="turn runtime did not drain"):
            process._reclaim_owned_runtime()
    finally:
        monkeypatch.undo()
        process._stopping.set()
        process._io_requests.put_nowait(_STOP)
        release.set()
        original_drain(2.0)
        process._thread.join(timeout=2.0)


def test_twenty_stuck_native_generations_only_leave_native_and_executor_threads() -> None:
    baseline = threading.active_count()
    processes: list[tuple[StreamingTurnProcess, threading.Event]] = []
    cycles = 20
    try:
        for index in range(cycles):
            process, release = _held_process(
                f"stuck-native-generation-{index}", observer=lambda _failure: None
            )
            processes.append((process, release))
            with pytest.raises(
                RuntimeError, match="did not stop after forced cleanup"
            ):
                process.stop()

        # A genuinely stuck native generation retains its native thread and the
        # asyncio executor thread waiting for it. The reclaimed observer, relay,
        # and turn actors must not add another three threads per failed cycle.
        assert threading.active_count() - baseline <= (2 * cycles) + 2
    finally:
        for process, release in processes:
            _cleanup(process, release)


def test_reclaim_does_not_hold_the_admission_lock_while_offering_the_sentinel() -> None:
    """The observer's exit check needs the admission lock, so offering the stop
    sentinel to a full queue must not hold that lock: otherwise reclaim and the
    just-released observer wait on each other until the stop deadline."""

    holding = threading.Event()
    release_observer = threading.Event()
    seen: list[str] = []

    def observer(failure: str) -> None:
        seen.append(failure)
        if failure == "first":
            holding.set()
            release_observer.wait(5.0)

    entered = threading.Event()
    release = threading.Event()
    process = StreamingTurnProcess(
        harness="pi",
        label="sentinel lock order",
        client_factory=lambda: _HeldEnterClient(entered, release),
        thread_name="sentinel-lock-order",
        stop_timeout_seconds=2.0,
        force_stop=lambda: None,
        force_stopped=lambda: False,
        force_stop_join_seconds=0.01,
        on_turn_failure=observer,
    )
    assert entered.wait(timeout=2.0)
    observer_thread = process._observer_thread
    observer_queue = process._observer_queue
    assert observer_thread is not None and observer_queue is not None
    errors: list[BaseException] = []
    try:
        process._dispatch_turn_failure("first")
        assert holding.wait(timeout=2.0)
        while not observer_queue.full():
            process._dispatch_turn_failure("second")
        queued = observer_queue.qsize()

        def reclaim() -> None:
            try:
                process._reclaim_owned_runtime()
            except BaseException as error:  # noqa: BLE001 - asserted below
                errors.append(error)

        reclaimer = threading.Thread(target=reclaim, name="sentinel-lock-reclaim")
        reclaimer.start()
        deadline = threading.Event()
        deadline.wait(0.3)  # reclaim is now offering the sentinel to the full queue
        release_observer.set()
        reclaimer.join(timeout=1.5)

        assert not reclaimer.is_alive(), "reclaim waited out its whole stop deadline"
        assert errors == []
        assert not observer_thread.is_alive()
        assert seen == ["first"] + ["second"] * queued
    finally:
        release_observer.set()
        _cleanup(process, release)
