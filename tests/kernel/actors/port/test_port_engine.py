# Decision record (file header; no inline comments by repository rule):
# - These cases pin the P2c public port object contract before its
#   implementation exists: create_engine / create_actor_host / ActorHost,
#   ActorSpawner -> ActorCell, ActorSpec.event_sink routing, ask, and per-cell
#   drain. They deliberately use only the public port names from
#   kernel.impl.actor_runtime.port so the shipped guardian remains an
#   implementation detail of the port.
# - Event routing asserts order (spec first, engine second), complete
#   delivery to both sinks, and BaseException isolation of either sink.
# - drain assertions separate the per-cell scope (other cells still admit)
#   from engine-wide drain (spawn refused afterwards, permanently).
"""Public actor port: factories, cells, event routing, ask, and drain."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

import hyprial.kernel.impl.actor_runtime.port.host as port_host
from hyprial.kernel.impl.actor_runtime import (
    ActorAdmissionError,
    ActorEvent,
    ActorFailedError,
    ActorHandle,
    ActorSpec,
    ActorState,
    AdmissionResult,
    ExpectedActorError,
    STATE_AUTHORITY,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted, EffectRequest
from hyprial.kernel.impl.actor_runtime.port import (
    ActorCell,
    ActorEngine,
    ActorEngineConfig,
    ActorHost,
    ActorSpawner,
    create_actor_host,
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


def _config(
    *, max_restarts: int = 2, event_sink: Callable[[ActorEvent], None] | None = None
) -> ActorEngineConfig:
    return ActorEngineConfig(policies=_policies(max_restarts), event_sink=event_sink)


def _echo(handled: list[object]) -> Callable[[object], object]:
    def handle(command: object) -> object:
        handled.append(command)
        return command

    return handle


def test_create_engine_exposes_the_actor_engine_protocol() -> None:
    handled: list[object] = []
    engine = create_engine(_config())
    assert isinstance(engine, ActorEngine)
    handle = engine.spawn(
        ActorSpec(name="engine-cell", handler_factory=lambda: _echo(handled))
    )
    assert isinstance(handle, ActorHandle)
    assert engine.tell(handle, "one") is AdmissionResult.ACCEPTED
    assert engine.ask(handle, "two", 1.0) == "two"
    _wait_until(lambda: handled == ["one", "two"])
    snapshot = engine.snapshot(handle)
    assert snapshot.handle == handle
    assert snapshot.state is ActorState.RUNNING
    assert snapshot.generation == 1
    assert engine.stop(handle, 1.0) is True
    assert engine.snapshot(handle).state is ActorState.STOPPED
    assert engine.drain(None, 1.0).complete is True
    with pytest.raises(RuntimeError):
        engine.spawn(
            ActorSpec(name="after-drain", handler_factory=lambda: _echo([]))
        )
    assert engine.shutdown(1.0) is True


def test_create_actor_host_spawns_cells_and_owns_host_capability() -> None:
    host = create_actor_host(_config())
    assert isinstance(host, ActorHost)
    assert isinstance(host, ActorSpawner)
    handled: list[object] = []
    cell = host.spawn(
        ActorSpec(name="host-cell", handler_factory=lambda: _echo(handled))
    )
    assert isinstance(cell, ActorCell)
    assert cell.handle.name == "host-cell"
    assert cell.tell("one") is AdmissionResult.ACCEPTED
    assert cell.ask("two", timeout=1.0) == "two"
    assert cell.snapshot().generation == 1
    assert cell.stop(1.0) is True
    assert cell.snapshot().state is ActorState.STOPPED

    peer = host.spawn(
        ActorSpec(name="host-peer", handler_factory=lambda: _echo(handled))
    )
    assert host.drain_all(1.0).complete is True
    assert peer.snapshot().state is ActorState.STOPPED
    assert host.shutdown(1.0) is True


def test_create_actor_host_uses_the_engine_factory() -> None:
    sentinel = object()
    seen: list[ActorEngineConfig] = []

    def fake_create_engine(config: ActorEngineConfig) -> object:
        seen.append(config)
        return sentinel

    config = _config()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(port_host, "create_engine", fake_create_engine)
        host = port_host.create_actor_host(config)
    assert seen == [config]
    assert host.engine is sentinel


def test_spawn_validation_and_engine_lifecycle_refusals() -> None:
    host = create_actor_host(_config())
    with pytest.raises(ValueError):
        host.spawn(
            ActorSpec(
                name="bad-profile",
                handler_factory=lambda: _echo([]),
                supervision_profile="not-a-profile",
            )
        )
    assert host.drain_all(1.0).complete is True
    with pytest.raises(RuntimeError):
        host.spawn(ActorSpec(name="refused", handler_factory=lambda: _echo([])))
    assert host.shutdown(1.0) is True


def test_actor_spec_event_sink_routes_spec_then_engine() -> None:
    order: list[tuple[str, str, str]] = []

    def engine_sink(event: ActorEvent) -> None:
        order.append(("engine", event.kind.value, event.handle.actor_id))

    engine = create_engine(_config(event_sink=engine_sink))

    def spec_sink(event: ActorEvent) -> None:
        order.append(("spec", event.kind.value, event.handle.actor_id))

    handle = engine.spawn(
        ActorSpec(
            name="routed",
            handler_factory=lambda: _echo([]),
            event_sink=spec_sink,
        )
    )
    assert engine.tell(handle, "go") is AdmissionResult.ACCEPTED
    _wait_until(lambda: len(order) >= 2)
    assert order[:2] == [
        ("spec", "command_completed", handle.actor_id),
        ("engine", "command_completed", handle.actor_id),
    ]
    assert engine.shutdown(1.0) is True


def test_event_sink_failures_are_isolated_per_sink() -> None:
    engine_events: list[str] = []
    engine = create_engine(
        _config(
            event_sink=lambda event: engine_events.append(event.kind.value)
        )
    )

    def raising_spec_sink(event: ActorEvent) -> None:
        del event
        raise SystemExit("spec sink failure")

    handle = engine.spawn(
        ActorSpec(
            name="spec-raises",
            handler_factory=lambda: _echo([]),
            event_sink=raising_spec_sink,
        )
    )
    assert engine.tell(handle, "go") is AdmissionResult.ACCEPTED
    _wait_until(lambda: engine_events == ["command_completed"])
    assert engine.shutdown(1.0) is True

    spec_events: list[str] = []

    def raising_engine_sink(event: ActorEvent) -> None:
        del event
        raise SystemExit("engine sink failure")

    def recording_spec_sink(event: ActorEvent) -> None:
        spec_events.append(event.kind.value)

    engine = create_engine(_config(event_sink=raising_engine_sink))
    handle = engine.spawn(
        ActorSpec(
            name="engine-raises",
            handler_factory=lambda: _echo([]),
            event_sink=recording_spec_sink,
        )
    )
    assert engine.tell(handle, "go") is AdmissionResult.ACCEPTED
    _wait_until(lambda: spec_events == ["command_completed"])
    assert engine.shutdown(1.0) is True


def test_host_timers_and_effect_lane_factory() -> None:
    host = create_actor_host(_config())
    handled: list[object] = []
    cell = host.spawn(
        ActorSpec(name="host-timer", handler_factory=lambda: _echo(handled))
    )
    timer_id = host.timers().send_after(cell.handle, 0.01, "tick")
    _wait_until(lambda: handled == ["tick"])
    assert host.timers().cancel_after(timer_id) is False
    lane_holder: list = []

    def complete(completion: EffectCompleted) -> AdmissionResult:
        lane_holder[0].acknowledge(
            completion.operation_id, completion.generation
        )
        return AdmissionResult.ACCEPTED

    lane = host.effect_lane(
        name="host-lane", execute=lambda value: value + 1, complete=complete
    )
    lane_holder.append(lane)
    assert lane.submit(EffectRequest("op-1", 1, 1)) is AdmissionResult.ACCEPTED
    _wait_until(lambda: lane.snapshot().outstanding == 0)
    assert lane.close(1.0) is True
    assert host.shutdown(1.0) is True


def test_ask_returns_identity_timeout_admission_expected_and_failure() -> None:
    release = threading.Event()
    entered = threading.Event()
    expected = ExpectedActorError("E_ASK", "expected rejection")

    def handler(command: object) -> object:
        if command == "block":
            entered.set()
            assert release.wait(2.0)
            return "late"
        if command == "expected":
            raise expected
        if command == "fail":
            raise RuntimeError("boom")
        return command

    engine = create_engine(_config(max_restarts=0))
    handle = engine.spawn(
        ActorSpec(name="asker", handler_factory=lambda: handler, mailbox_capacity=1)
    )
    value = object()
    assert engine.ask(handle, value, 1.0) is value
    with pytest.raises(TimeoutError):
        engine.ask(handle, "block", 0.05)
    try:
        assert entered.wait(1.0)
        assert engine.tell(handle, "filler") is AdmissionResult.ACCEPTED
        with pytest.raises(ActorAdmissionError) as overloaded:
            engine.ask(handle, "overflow", 0.05)
        assert overloaded.value.args[0] == "overloaded"
    finally:
        release.set()
    _wait_until(lambda: engine.snapshot(handle).in_flight == 0)
    with pytest.raises(ExpectedActorError) as rejected:
        engine.ask(handle, "expected", 1.0)
    assert rejected.value is expected
    assert engine.snapshot(handle).state is ActorState.RUNNING
    with pytest.raises(ActorFailedError):
        engine.ask(handle, "fail", 1.0)
    _wait_until(lambda: engine.snapshot(handle).state is ActorState.QUARANTINED)
    with pytest.raises(ActorAdmissionError) as closed:
        engine.ask(handle, "after-quarantine", 0.1)
    assert closed.value.args[0] == "closed"
    assert engine.shutdown(1.0) is True


def test_actor_cell_ask_on_a_worker_pool_is_closed() -> None:
    from hyprial.kernel.impl.actor_runtime.port import ActorPoolSpec

    host = create_actor_host(_config())
    pool = host.spawn_pool(
        ActorPoolSpec(name="pool-ask", handler_factory=lambda: lambda value: value)
    )
    with pytest.raises(ActorAdmissionError) as closed:
        pool.ask("value", timeout=0.1)
    assert closed.value.args[0] == "closed"
    assert host.shutdown(1.0) is True


def test_actor_cell_drain_is_scoped_to_one_cell() -> None:
    host = create_actor_host(_config())
    handled: list[tuple[str, object]] = []

    def handler(name: str) -> Callable[[object], object]:
        return lambda command: handled.append((name, command))

    first = host.spawn(
        ActorSpec(name="first", handler_factory=lambda: handler("first"))
    )
    second = host.spawn(
        ActorSpec(name="second", handler_factory=lambda: handler("second"))
    )
    report = first.drain(1.0)
    assert report.complete is True
    assert report.remaining == ()
    assert first.snapshot().state is ActorState.STOPPED
    assert second.snapshot().state is ActorState.RUNNING
    assert first.tell("refused") is AdmissionResult.CLOSED
    assert second.tell("accepted") is AdmissionResult.ACCEPTED
    _wait_until(lambda: handled == [("second", "accepted")])
    assert host.drain_all(1.0).complete is True
    assert second.snapshot().state is ActorState.STOPPED
    assert host.shutdown(1.0) is True


def test_actor_cell_drain_honours_its_deadline_and_can_be_retried() -> None:
    host = create_actor_host(_config())
    release = threading.Event()
    entered = threading.Event()

    def blocker(command: object) -> None:
        if command == "block":
            entered.set()
            assert release.wait(2.0)

    first = host.spawn(ActorSpec(name="blocked", handler_factory=lambda: blocker))
    peer = host.spawn(
        ActorSpec(name="peer", handler_factory=lambda: _echo([]))
    )
    assert first.tell("block") is AdmissionResult.ACCEPTED
    try:
        assert entered.wait(1.0)
        report = first.drain(0.03)
        assert report.complete is False
        assert [handle.name for handle in report.remaining] == ["blocked"]
        assert first.tell("refused") is AdmissionResult.CLOSED
        assert peer.tell("still-open") is AdmissionResult.ACCEPTED
    finally:
        release.set()
    _wait_until(lambda: first.snapshot().in_flight == 0)
    assert first.drain(1.0).complete is True
    assert first.snapshot().state is ActorState.STOPPED
    assert host.shutdown(1.0) is True
