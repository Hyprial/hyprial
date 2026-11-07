"""Focused Agent migration effect custody and owner-mailbox controls."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import hyprial.identity.impl.agents.actor._core as actor_module
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.identity.impl.agents.migration.actor import (
    MigrationOutcome,
    MigrationOwnerCall,
)
from hyprial.identity.impl.agents.actor.ports import UpdateAgentCommand
from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected


def _until(predicate: Callable[[], bool], *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def migration_actor(tmp_path: Path):
    registry = AgentRegistry(
        tmp_path / "agents.sqlite3",
        owner="op",
        machine="node",
        hyprial_home=tmp_path / "home",
    )
    registry.create("foo")
    registry.create("bar")
    events: list[object] = []
    actor = AgentActor(registry, event_sink=events.append)
    try:
        yield actor, registry, events
    finally:
        actor.drain(3.0)
        registry.close()


def _start_call(call: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    settled: list[object] = []

    def run() -> None:
        try:
            settled.append(call())
        except BaseException as error:
            settled.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, settled


def test_accepted_owner_update_survives_overloaded_retry(
    migration_actor: tuple[AgentActor, AgentRegistry, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _registry, _events = migration_actor
    accepted = threading.Event()
    retried = threading.Event()
    saved: list[MigrationOwnerCall] = []
    original_tell = actor._runtime.tell
    original_snapshot = actor._runtime.snapshot
    effect_errors: list[BaseException] = []

    def intercepted_tell(handle: object, command: object) -> AdmissionResult:
        if isinstance(command, MigrationOwnerCall):
            if not saved:
                saved.append(command)
                accepted.set()
                return AdmissionResult.ACCEPTED
            retried.set()
            return AdmissionResult.OVERLOADED
        return original_tell(handle, command)

    def snapshot_after_restart(handle: object) -> object:
        snapshot = original_snapshot(handle)
        return (
            dataclasses.replace(snapshot, generation=snapshot.generation + 1)
            if accepted.is_set()
            else snapshot
        )

    def effect(work: Any, owner: AgentActor) -> MigrationOutcome:
        try:
            response = owner.migration_owner_call(
                MigrationOwnerCall(
                    work.request.operation_id,
                    "migration-update",
                    "update-config",
                    work.agent.actor,
                    work.agent.entity_token,
                    work.agent.config,
                )
            )
            if response.failure is not None:
                raise response.failure.to_exception()
            return MigrationOutcome.success({"ok": True})
        except BaseException as error:
            effect_errors.append(error)
            return MigrationOutcome.failed(error)

    monkeypatch.setattr(actor_module, "run_migration_effect", effect)
    monkeypatch.setattr(actor._runtime, "tell", intercepted_tell)
    monkeypatch.setattr(actor._runtime, "snapshot", snapshot_after_restart)
    waiter, settled = _start_call(
        lambda: actor.run_migration("agent.migrate.execute", "foo")
    )
    try:
        assert accepted.wait(3), "owner update was not accepted"
        assert retried.wait(3), "generation-changed retry was not attempted"
        assert not effect_errors, "retry overload caused premature compensation"
        assert waiter.is_alive(), "migration returned before accepted owner update"
        monkeypatch.setattr(actor._runtime, "tell", original_tell)
        monkeypatch.setattr(actor._runtime, "snapshot", original_snapshot)
        assert original_tell(actor._handle, saved[0]) is AdmissionResult.ACCEPTED
        waiter.join(3)
        assert not waiter.is_alive()
        assert settled == [{"ok": True}]
    finally:
        monkeypatch.setattr(actor._runtime, "tell", original_tell)
        monkeypatch.setattr(actor._runtime, "snapshot", original_snapshot)
        if saved and waiter.is_alive():
            original_tell(actor._handle, saved[0])
        waiter.join(3)


def test_migration_fences_target_while_unrelated_agent_progresses(
    migration_actor: tuple[AgentActor, AgentRegistry, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _registry, events = migration_actor
    entered = threading.Event()
    release = threading.Event()

    def held_effect(_work: Any, _owner: AgentActor) -> MigrationOutcome:
        entered.set()
        assert release.wait(3)
        return MigrationOutcome.success({"ok": True})

    monkeypatch.setattr(actor_module, "run_migration_effect", held_effect)
    waiter, settled = _start_call(
        lambda: actor.run_migration("agent.migrate.preflight", "foo")
    )
    try:
        assert entered.wait(3)
        assert actor.submit(UpdateAgentCommand("bar-update", "bar")) is PortAdmission.ACCEPTED
        assert actor.submit(UpdateAgentCommand("foo-update", "foo")) is PortAdmission.ACCEPTED
        assert _until(
            lambda: {getattr(event, "correlation_id", None) for event in events}
            >= {"bar-update", "foo-update"}
        )
        assert not any(
            isinstance(event, PortCommandRejected)
            and event.correlation_id == "bar-update"
            for event in events
        )
        assert any(
            isinstance(event, PortCommandRejected)
            and event.correlation_id == "foo-update"
            and event.code == "AGENT_MIGRATION_BUSY"
            for event in events
        )
    finally:
        release.set()
        waiter.join(3)
    assert settled == [{"ok": True}]


def test_migration_capacity_stays_pinned_until_live_consumer_reads_result(
    migration_actor: tuple[AgentActor, AgentRegistry, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _registry, _events = migration_actor
    actor._migration_call_capacity = 1
    decoding = threading.Event()
    release = threading.Event()
    original_result = MigrationOutcome.result

    def held_result(outcome: MigrationOutcome) -> dict[str, object]:
        decoding.set()
        assert release.wait(3)
        return original_result(outcome)

    monkeypatch.setattr(
        actor_module,
        "run_migration_effect",
        lambda _work, _owner: MigrationOutcome.success({"ok": True}),
    )
    monkeypatch.setattr(MigrationOutcome, "result", held_result)
    waiter, settled = _start_call(
        lambda: actor.run_migration("agent.migrate.preflight", "foo")
    )
    try:
        assert decoding.wait(3)
        with pytest.raises(Exception, match="capacity"):
            actor.run_migration("agent.migrate.preflight", "bar", timeout=0)
        assert len(actor._migration_waiters) == 1
    finally:
        release.set()
        waiter.join(3)
    assert settled == [{"ok": True}]


def test_failed_actor_start_closes_migration_effect_thread(tmp_path: Path) -> None:
    registry = AgentRegistry(
        tmp_path / "agents.sqlite3",
        owner="op",
        machine="node",
        hyprial_home=tmp_path / "home",
    )

    class RefusingRuntime:
        def start(self, _spec: object) -> None:
            raise RuntimeError("synthetic actor start refusal")

    actor = AgentActor.__new__(AgentActor)
    try:
        with pytest.raises(RuntimeError, match="synthetic actor start refusal"):
            actor.__init__(
                registry, event_sink=lambda _event: None, runtime=RefusingRuntime()
            )
        assert _until(
            lambda: not any(
                thread.is_alive() for thread in actor._migration_effects._threads
            ),
            timeout=1.0,
        )
    finally:
        if hasattr(actor, "_migration_effects"):
            actor._migration_effects.close(0.1)
        if getattr(actor, "_home_effects", None) is not None:
            actor._home_effects.close(0.1)
        if getattr(actor, "_state_effects", None) is not None:
            actor._state_effects.close(0.1)
        registry.close()


def test_terminal_migration_result_is_detached_on_replay(
    migration_actor: tuple[AgentActor, AgentRegistry, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _registry, _events = migration_actor
    calls: list[str] = []

    def effect(work: Any, _owner: AgentActor) -> MigrationOutcome:
        calls.append(work.request.operation_id)
        return MigrationOutcome.success(
            {"ok": True, "nested": {"items": ["original"]}}
        )

    monkeypatch.setattr(actor_module, "run_migration_effect", effect)
    first = actor.run_migration(
        "agent.migrate.preflight", "foo", operation_id="fixed-operation"
    )
    first["nested"]["items"].append("poison")
    second = actor.run_migration(
        "agent.migrate.preflight", "foo", operation_id="fixed-operation"
    )
    assert second == {"ok": True, "nested": {"items": ["original"]}}
    assert calls == ["fixed-operation"]
    for error in (ValueError("bad manifest"), OSError("plan unreadable")):
        failure = MigrationOutcome.failed(error)
        with pytest.raises(type(error), match=str(error)):
            failure.result()


def test_timed_out_caller_keeps_accepted_effect_and_can_join(
    migration_actor: tuple[AgentActor, AgentRegistry, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _registry, _events = migration_actor
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def held_effect(work: Any, _owner: AgentActor) -> MigrationOutcome:
        calls.append(work.request.operation_id)
        entered.set()
        assert release.wait(3)
        return MigrationOutcome.success({"ok": True})

    monkeypatch.setattr(actor_module, "run_migration_effect", held_effect)
    try:
        with pytest.raises(TimeoutError, match="timeout-id"):
            actor.run_migration(
                "agent.migrate.preflight", "foo",
                operation_id="timeout-id", timeout=0.05,
            )
        assert entered.is_set()
        assert not actor.drain(0.05).complete
        release.set()
        assert actor.run_migration(
            "agent.migrate.preflight", "foo", operation_id="timeout-id"
        ) == {"ok": True}
        assert calls == ["timeout-id"]
    finally:
        release.set()


def test_sequential_owner_callbacks_do_not_accumulate_responses(
    migration_actor: tuple[AgentActor, AgentRegistry, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, _registry, _events = migration_actor
    cached: list[int] = []

    def effect(work: Any, owner: AgentActor) -> MigrationOutcome:
        for index in range(240):
            response = owner.migration_owner_call(
                MigrationOwnerCall(
                    work.request.operation_id,
                    f"sequential-{index}",
                    "snapshot",
                    work.agent.actor,
                    work.agent.entity_token,
                )
            )
            assert response.failure is None
            assert response.value.actor == work.agent.actor
            cached.append(len(owner._migration_owner_responses))
        return MigrationOutcome.success({"ok": True})

    monkeypatch.setattr(actor_module, "run_migration_effect", effect)
    assert actor.run_migration("agent.migrate.preflight", "foo") == {"ok": True}
    assert max(cached) <= 2
    assert actor._migration_owner_responses == {}
