# Decision record (file header; no inline comments by repository rule):
# - Timers are pinned at the engine port (send_after / cancel_after), because
#   D3 moves GenerationScheduler behind the host factory. Cases assert
#   schedule-time validation, generation fencing, terminal dead letters,
#   overload retry, and cancellation at stop acceptance / drain / shutdown.
# - Pools are pinned as custody operations over the shipped EffectLane: one
#   logical actor, tokens keyed by (operation_id, generation), completion as
#   internal admission to the owner, OwnerGone dead letters, and joint drain
#   that counts a live token as not idle.
"""Public actor port: send_after and worker-pool custody."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from hyprial.kernel.impl.actor_runtime import (
    ActorHandle,
    ActorSpec,
    ActorState,
    AdmissionResult,
    STATE_AUTHORITY,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
from hyprial.kernel.impl.actor_runtime.port import (
    ActorEngineConfig,
    ActorPoolSpec,
    create_engine,
)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _policies(max_restarts: int = 2) -> dict[str, SupervisionPolicy]:
    return {
        STATE_AUTHORITY: SupervisionPolicy(
            max_restarts=max_restarts,
            restart_window=10.0,
            base_backoff=0.0,
            max_backoff=0.0,
            jitter_ratio=0.0,
        )
    }


def _config(*, max_restarts: int = 2) -> ActorEngineConfig:
    return ActorEngineConfig(policies=_policies(max_restarts))


def _recorder(log: list[object]) -> Callable[[object], object]:
    def handle(command: object) -> object:
        log.append(command)
        return command

    return handle


def test_send_after_delivers_cancels_and_validates() -> None:
    log: list[object] = []
    dead: list[tuple[object, str]] = []
    engine = create_engine(_config())
    handle = engine.spawn(
        ActorSpec(
            name="timer-basic",
            handler_factory=lambda: _recorder(log),
            undelivered_sink=lambda command, reason: dead.append((command, reason)),
        )
    )
    timer_id = engine.send_after(handle, 0.01, "hello")
    assert isinstance(timer_id, str)
    _wait_until(lambda: log == ["hello"])
    assert engine.cancel_after("missing") is False
    cancelled = engine.send_after(handle, 0.2, "cancelled")
    assert engine.cancel_after(cancelled) is True
    assert engine.cancel_after(cancelled) is False
    time.sleep(0.05)
    assert log == ["hello"]
    assert dead == []
    with pytest.raises(ValueError):
        engine.send_after(handle, -0.1, "negative")
    with pytest.raises(KeyError):
        engine.send_after(ActorHandle("forged", "timer-basic"), 0.0, "forged")
    pool = engine.spawn_pool(
        ActorPoolSpec(name="timer-pool", handler_factory=lambda: lambda value: value)
    )
    with pytest.raises(ValueError):
        engine.send_after(pool, 0.0, "pool-target")
    assert engine.shutdown(1.0) is True


def test_send_after_uses_schedule_time_generation_fencing() -> None:
    log: list[object] = []
    dead: list[tuple[object, str]] = []
    engine = create_engine(_config())

    def handler(command: object) -> object:
        if command == "crash":
            raise RuntimeError("boom")
        log.append(command)
        return command

    handle = engine.spawn(
        ActorSpec(
            name="timer-fenced",
            handler_factory=lambda: handler,
            undelivered_sink=lambda command, reason: dead.append((command, reason)),
        )
    )
    engine.send_after(handle, 0.2, "stale-timer")
    assert engine.tell(handle, "crash") is AdmissionResult.ACCEPTED
    _wait_until(lambda: engine.snapshot(handle).generation == 2)
    time.sleep(0.3)
    assert log == []
    assert dead == []
    assert engine.shutdown(1.0) is True


def test_send_after_dead_letters_terminal_states_and_retries_overload() -> None:
    dead: list[tuple[object, str]] = []
    engine = create_engine(_config(max_restarts=0))
    handle = engine.spawn(
        ActorSpec(
            name="timer-terminal",
            handler_factory=lambda: _recorder([]),
            undelivered_sink=lambda command, reason: dead.append((command, reason)),
        )
    )
    assert engine.stop(handle, 1.0) is True
    engine.send_after(handle, 0.01, "after-stop")
    _wait_until(lambda: dead == [("after-stop", "stopped")])
    assert engine.shutdown(1.0) is True

    dead.clear()
    engine = create_engine(_config(max_restarts=0))
    handle = engine.spawn(
        ActorSpec(
            name="timer-quarantined",
            handler_factory=lambda: lambda command: (_ for _ in ()).throw(
                RuntimeError("boom")
            ),
            undelivered_sink=lambda command, reason: dead.append((command, reason)),
        )
    )
    assert engine.tell(handle, "fail") is AdmissionResult.ACCEPTED
    _wait_until(lambda: engine.snapshot(handle).state is ActorState.QUARANTINED)
    engine.send_after(handle, 0.01, "after-quarantine")
    _wait_until(lambda: dead == [("after-quarantine", "quarantined")])
    assert engine.shutdown(1.0) is True

    release = threading.Event()
    entered = threading.Event()
    log: list[object] = []

    def blocking(command: object) -> None:
        if command == "block":
            entered.set()
            assert release.wait(2.0)
        log.append(command)

    engine = create_engine(_config())
    handle = engine.spawn(
        ActorSpec(
            name="timer-overload",
            handler_factory=lambda: blocking,
            mailbox_capacity=1,
        )
    )
    assert engine.tell(handle, "block") is AdmissionResult.ACCEPTED
    try:
        assert entered.wait(1.0)
        assert engine.tell(handle, "filler") is AdmissionResult.ACCEPTED
        engine.send_after(handle, 0.01, "retried")
    finally:
        release.set()
    _wait_until(lambda: log == ["block", "filler", "retried"], timeout=3.0)
    assert engine.shutdown(1.0) is True


def test_send_after_cancelled_at_stop_acceptance_and_drain_start() -> None:
    log: list[object] = []
    dead: list[tuple[object, str]] = []
    engine = create_engine(_config())
    handle = engine.spawn(
        ActorSpec(
            name="timer-cancelled",
            handler_factory=lambda: _recorder(log),
            undelivered_sink=lambda command, reason: dead.append((command, reason)),
        )
    )
    engine.send_after(handle, 0.2, "stop-cancelled")
    assert engine.stop(handle, 1.0) is True
    time.sleep(0.3)
    assert log == []
    assert dead == []

    handle = engine.spawn(
        ActorSpec(
            name="timer-drained",
            handler_factory=lambda: _recorder(log),
            undelivered_sink=lambda command, reason: dead.append((command, reason)),
        )
    )
    engine.send_after(handle, 0.2, "drain-cancelled")
    assert engine.drain((handle,), 1.0).complete is True
    time.sleep(0.3)
    assert log == []
    assert dead == []
    assert engine.shutdown(1.0) is True


def _owner(
    engine, pool: ActorHandle, completions: list[EffectCompleted], ack: bool = True
) -> Callable[[object], object]:
    def handler(message: object) -> object:
        if isinstance(message, EffectCompleted):
            completions.append(message)
            if ack:
                return engine.pool_acknowledge(
                    pool, message.operation_id, message.generation
                )
            return None
        return message() if callable(message) else message

    return handler


def test_worker_pool_completion_custody_and_acknowledgement() -> None:
    engine = create_engine(_config())
    completions: list[EffectCompleted] = []
    leftovers: list[tuple[object, str]] = []
    pool = engine.spawn_pool(
        ActorPoolSpec(
            name="pool-custody",
            handler_factory=lambda: lambda payload: payload.upper(),
            pool_size=1,
            capacity=2,
            undelivered_sink=lambda message, reason: leftovers.append((message, reason)),
        )
    )
    owner = engine.spawn(
        ActorSpec(
            name="pool-owner",
            handler_factory=lambda: _owner(engine, pool, completions),
        )
    )
    assert (
        engine.pool_submit(pool, owner, "op-1", 1, "alpha")
        is AdmissionResult.ACCEPTED
    )
    _wait_until(lambda: len(completions) == 1)
    message = completions[0]
    assert (message.operation_id, message.generation, message.result) == (
        "op-1", 1, "ALPHA"
    )
    _wait_until(
        lambda: engine.pool_reserve(pool, "probe", 1) is AdmissionResult.ACCEPTED
    )
    assert engine.pool_cancel_reservation(pool, "probe", 1) is True
    assert engine.pool_reserve(pool, "pending", 1) is AdmissionResult.ACCEPTED
    assert (
        engine.pool_submit_reserved(pool, owner, "pending", 1, "beta")
        is AdmissionResult.ACCEPTED
    )
    _wait_until(lambda: len(completions) == 2)
    assert engine.pool_acknowledge(pool, "op-1", 1) is False
    assert engine.pool_acknowledge(pool, "pending", 1) is False
    assert engine.snapshot(pool).state is ActorState.RUNNING
    assert engine.snapshot(pool).generation == 1
    assert engine.stop(pool, 1.0) is True
    _wait_until(lambda: engine.snapshot(pool).state is ActorState.STOPPED)
    assert engine.shutdown(1.0) is True


def test_worker_pool_overload_cancellation_and_worker_errors() -> None:
    engine = create_engine(_config())
    completions: list[EffectCompleted] = []
    release = threading.Event()
    entered = threading.Event()

    def worker(payload: object) -> object:
        if payload == "block":
            entered.set()
            assert release.wait(2.0)
            return "blocked"
        if payload == "error":
            raise RuntimeError("worker failure")
        return payload

    pool = engine.spawn_pool(
        ActorPoolSpec(
            name="pool-overload",
            handler_factory=lambda: worker,
            pool_size=1,
            capacity=2,
        )
    )
    owner = engine.spawn(
        ActorSpec(
            name="pool-overload-owner",
            handler_factory=lambda: _owner(engine, pool, completions),
        )
    )
    assert (
        engine.pool_submit(pool, owner, "running", 1, "block")
        is AdmissionResult.ACCEPTED
    )
    assert engine.pool_reserve(pool, "reserved", 1) is AdmissionResult.ACCEPTED
    assert (
        engine.pool_submit_reserved(pool, owner, "reserved", 1, "queued")
        is AdmissionResult.ACCEPTED
    )
    try:
        assert entered.wait(1.0)
        assert (
            engine.pool_reserve(pool, "overflow", 1)
            is AdmissionResult.OVERLOADED
        )
        assert engine.pool_submit(pool, owner, "overflow", 1, "x") is AdmissionResult.OVERLOADED
        assert engine.pool_cancel_reservation(pool, "reserved", 1) is False
    finally:
        release.set()
    _wait_until(lambda: len(completions) == 2)
    assert {message.operation_id for message in completions} == {"running", "reserved"}
    assert (
        engine.pool_submit(pool, owner, "error", 1, "error")
        is AdmissionResult.ACCEPTED
    )
    _wait_until(lambda: len(completions) == 3)
    failed = next(message for message in completions if message.operation_id == "error")
    assert (failed.error, failed.result) == ("RuntimeError", None)
    _wait_until(
        lambda: engine.pool_reserve(pool, "probe", 1) is AdmissionResult.ACCEPTED
    )
    assert engine.pool_cancel_reservation(pool, "probe", 1) is True
    assert engine.shutdown(1.0) is True


def test_worker_pool_drain_counts_live_tokens_and_owner_gone() -> None:
    engine = create_engine(_config())
    completions: list[EffectCompleted] = []
    dead: list[tuple[EffectCompleted, str]] = []
    pool = engine.spawn_pool(
        ActorPoolSpec(
            name="pool-drain",
            handler_factory=lambda: lambda payload: payload,
            pool_size=1,
            capacity=1,
            undelivered_sink=lambda message, reason: dead.append((message, reason)),
        )
    )
    owner = engine.spawn(
        ActorSpec(
            name="pool-drain-owner",
            handler_factory=lambda: _owner(engine, pool, completions, ack=False),
        )
    )
    assert engine.pool_submit(pool, owner, "unacked", 1, "value") is AdmissionResult.ACCEPTED
    _wait_until(lambda: len(completions) == 1)
    assert engine.pool_reserve(pool, "probe", 1) is AdmissionResult.OVERLOADED
    report = engine.drain((pool,), 0.05)
    assert report.complete is False
    assert report.remaining == (pool,)
    assert engine.pool_acknowledge(pool, "unacked", 1) is True
    assert engine.pool_acknowledge(pool, "unacked", 1) is False
    assert engine.drain((pool,), 1.0).complete is True
    assert engine.snapshot(pool).state is ActorState.STOPPED
    assert engine.shutdown(1.0) is True


def test_worker_pool_dead_letters_a_completion_whose_owner_is_gone() -> None:
    engine = create_engine(_config())
    completions: list[EffectCompleted] = []
    dead: list[tuple[EffectCompleted, str]] = []
    pool = engine.spawn_pool(
        ActorPoolSpec(
            name="pool-owner-gone",
            handler_factory=lambda: lambda payload: payload,
            pool_size=1,
            capacity=1,
            undelivered_sink=lambda message, reason: dead.append((message, reason)),
        )
    )
    owner = engine.spawn(
        ActorSpec(
            name="pool-gone-owner",
            handler_factory=lambda: _owner(engine, pool, completions, ack=False),
        )
    )
    assert engine.stop(owner, 1.0) is True
    assert engine.pool_submit(pool, owner, "gone", 1, "value") is AdmissionResult.ACCEPTED
    _wait_until(lambda: len(dead) == 1)
    message, reason = dead[0]
    assert (message.operation_id, message.generation, message.result) == (
        "gone", 1, "value"
    )
    assert reason == "stopped"
    assert engine.pool_reserve(pool, "probe", 1) is AdmissionResult.ACCEPTED
    assert engine.pool_cancel_reservation(pool, "probe", 1) is True
    assert engine.shutdown(1.0) is True
