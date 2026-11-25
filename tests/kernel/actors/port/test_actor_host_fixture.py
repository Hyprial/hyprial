"""The P5 ``actor_host`` fixture and the isolation contract it rides on."""

from __future__ import annotations

from collections.abc import Callable

from hyprial.kernel.impl.actor_runtime import (
    ActorSpec,
    ActorState,
    AdmissionResult,
)
from hyprial.kernel.impl.actor_runtime.port import (
    ActorCell,
    ActorHost,
    create_actor_host,
)


def _echo(handled: list[object]) -> Callable[[object], object]:
    def handle(command: object) -> object:
        handled.append(command)
        return command

    return handle


def test_actor_host_fixture_spawns_a_cell(actor_host: ActorHost) -> None:
    handled: list[object] = []
    cell = actor_host.spawn(
        ActorSpec(name="fixture-cell", handler_factory=lambda: _echo(handled))
    )
    assert isinstance(cell, ActorCell)
    assert cell.handle.name == "fixture-cell"
    assert cell.tell("one") is AdmissionResult.ACCEPTED
    assert cell.ask("two", timeout=1.0) == "two"
    assert cell.stop(1.0) is True
    assert cell.snapshot().state is ActorState.STOPPED


def test_actor_host_fixture_drains_every_cell(actor_host: ActorHost) -> None:
    first = actor_host.spawn(
        ActorSpec(name="fixture-first", handler_factory=lambda: _echo([]))
    )
    second = actor_host.spawn(
        ActorSpec(name="fixture-second", handler_factory=lambda: _echo([]))
    )
    report = actor_host.drain_all(1.0)
    assert report.complete is True
    assert first.snapshot().state is ActorState.STOPPED
    assert second.snapshot().state is ActorState.STOPPED


def test_two_hosts_own_separate_engines(actor_host: ActorHost) -> None:
    other = create_actor_host()
    try:
        assert other.engine is not actor_host.engine
        handled: list[object] = []
        cell = actor_host.spawn(
            ActorSpec(name="kept-cell", handler_factory=lambda: _echo(handled))
        )
        assert other.drain_all(1.0).complete is True
        assert cell.snapshot().state is ActorState.RUNNING
    finally:
        assert other.shutdown(5.0) is True
