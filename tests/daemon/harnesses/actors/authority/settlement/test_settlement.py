from __future__ import annotations

import threading
import time
import pytest
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessReadyObserved, WaitHarnessReadyCommand
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime

from ..support import (
    _Process,
    _spec,
    _wait_for,
)


def test_dispatch_completion_lost_with_generation_is_replayed_without_reenqueue() -> None:
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()

    class _Streaming(_Process):
        def __init__(self) -> None:
            super().__init__()
            self.enqueues = 0

        def enqueue(self, _delivery: HarnessDelivery) -> bool:
            self.enqueues += 1
            enqueue_entered.set()
            assert release_enqueue.wait(2.0)
            return True

        def drain_results(self) -> tuple[HarnessResult, ...]:
            return ()

        def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
            del delivery_id, timeout
            return True

    class _StreamingLauncher:
        def __init__(self) -> None:
            self.process = _Streaming()

        def start(self, _spec: HarnessLaunchSpec) -> _Streaming:
            return self.process

    launcher = _StreamingLauncher()
    supervisor = ManagedHarnessRuntime(launcher)
    supervisor.start(_spec())
    actor = supervisor._actor
    io = actor._io
    assert io is not None
    first_completion_admitted = threading.Event()
    real_emit = io._emit

    def observe_emit(completion: object) -> tuple[AdmissionResult, int]:
        result = real_emit(completion)
        admission = result[0]
        if admission is AdmissionResult.ACCEPTED:
            first_completion_admitted.set()
        return result

    io._emit = observe_emit
    crash_once = True

    def fail_once(_event: object) -> None:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise RuntimeError("crash before queued completion")

    actor.subscribe_events(fail_once)
    delivery = HarnessDelivery("d1", "c1", "a", "worker", "hello")
    outcomes: list[bool] = []
    projection = actor.projection
    projection._lock.acquire()
    try:
        caller = threading.Thread(
            target=lambda: outcomes.append(supervisor.dispatch("worker", delivery)),
            daemon=True,
        )
        caller.start()
        assert enqueue_entered.wait(1.0)
        old_generation = actor.generation
        assert actor._admit(object()) is AdmissionResult.ACCEPTED
        release_enqueue.set()
        assert first_completion_admitted.wait(1.0)
    finally:
        projection._lock.release()

    _wait_for(lambda: actor.generation > old_generation)
    caller.join(2.0)
    assert outcomes == [True]
    assert launcher.process.enqueues == 1
    _wait_for(lambda: io.in_flight == 0)
    assert len(actor._calls) == 0
    supervisor.stop()


def test_held_completion_beyond_old_deadline_keeps_single_claim_and_custody() -> None:
    class _Streaming(_Process):
        def __init__(self) -> None:
            super().__init__()
            self.enqueues = 0

        def enqueue(self, _delivery: HarnessDelivery) -> bool:
            self.enqueues += 1
            return True

        def drain_results(self) -> tuple[HarnessResult, ...]:
            return ()

        def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
            del delivery_id, timeout
            return True

    class _StreamingLauncher:
        def __init__(self) -> None:
            self.process = _Streaming()

        def start(self, _spec: HarnessLaunchSpec) -> _Streaming:
            return self.process

    launcher = _StreamingLauncher()
    supervisor = ManagedHarnessRuntime(launcher)
    supervisor.start(_spec())
    actor = supervisor._actor
    io = actor._io
    assert io is not None
    real_emit = io._emit
    completion_admitted = threading.Event()
    admissions = 0

    def observe_emit(completion: object) -> tuple[AdmissionResult, int]:
        nonlocal admissions
        result = real_emit(completion)
        if result[0] is AdmissionResult.ACCEPTED:
            admissions += 1
            completion_admitted.set()
        return result

    io._emit = observe_emit
    outcomes: list[bool] = []
    caller = threading.Thread(
        target=lambda: outcomes.append(
            supervisor.dispatch(
                "worker", HarnessDelivery("held", "c", "a", "worker", "hello")
            )
        ),
        daemon=True,
    )
    actor.projection._lock.acquire()
    try:
        caller.start()
        assert completion_admitted.wait(1.0)
        time.sleep(2.1)
        assert admissions == 1
        assert launcher.process.enqueues == 1
        assert caller.is_alive()
        assert io.in_flight == 1
    finally:
        actor.projection._lock.release()

    caller.join(1.0)
    assert outcomes == [True]
    assert admissions == 1
    assert launcher.process.enqueues == 1
    _wait_for(lambda: io.in_flight == 0)
    supervisor.stop()


def test_reply_timeout_cancels_registry_and_late_completion_is_ignored() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _ReadyProcess(_Process):
        def wait_ready(self, *, timeout: float | None = None) -> bool:
            del timeout
            entered.set()
            return release.wait(2.0)

    class _ReadyLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _ReadyProcess:
            return _ReadyProcess()

    supervisor = ManagedHarnessRuntime(_ReadyLauncher())
    supervisor.start(_spec())
    def invoke() -> None:
        with pytest.raises(TimeoutError, match="reply deadline"):
            supervisor._facade._request(
                WaitHarnessReadyCommand(
                    "timeout-correlation", "codex", "worker", 2.0
                ),
                HarnessReadyObserved,
                timeout=0.01,
            )

    waiter = threading.Thread(target=invoke, daemon=True)
    waiter.start()
    assert entered.wait(1.0)
    _wait_for(lambda: len(supervisor._actor._calls) == 1)
    waiter.join(1.0)
    assert len(supervisor._facade._pending) == 0

    release.set()
    _wait_for(lambda: len(supervisor._actor._calls) == 0)
    assert len(supervisor._actor._calls) == 0
    supervisor.stop()
