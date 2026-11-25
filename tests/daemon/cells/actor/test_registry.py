from __future__ import annotations

import threading
from dataclasses import dataclass
from types import SimpleNamespace

from hyprial.daemon.impl.cells.actor.actor import document_digest
from hyprial.daemon.impl.cells.actor.contracts import (
    CellActorState,
    CellActorWakeup,
)

from hyprial.daemon.impl.cells.actor.registry import CellActorRegistry
from hyprial.daemon.impl.cells.actor.snapshot import (
    CellActorHistoryStore,
    CellActorSnapshotStore,
)
from hyprial.kernel import (
    ActorEngineConfig,
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorSpawnRefused,
    ActorState,
    AdmissionResult,
    STATE_AUTHORITY,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.contracts import ActorFailedError
from tests.daemon.cells.actor.actor_support import (
    BlockCommand,
    BlockingExtension,
    Log,
    PoisonExtension,
    agent_cell,
    agent_id,
    host as actor_host,
    wait_until,
)

CELL_GROUP = "cell"


@dataclass
class Runtime:
    cell: object | None
    stale: bool = False
    updated: int = 1
    retries: int = 0

    def lookup(self, _cell_id: str) -> object | None:
        return self.cell

    def health(self) -> object:
        return SimpleNamespace(source_stale=self.stale, detail="offline" if self.stale else None,
                               updated_at_ms=self.updated)

    def request_index_retry(self) -> None:
        self.retries += 1


class Actor:
    def __init__(self, spec: object, cell_id: str, host: "Host") -> None:
        self.spec = spec
        self.host = host
        self.handle = ActorHandle(f"actor-{cell_id}", cell_id, CELL_GROUP, cell_id)
        self.state = ActorState.RUNNING
        self.generation = 1
        self.handler = spec.handler_factory()
        self.tell_count = 0
        self.stop_count = 0

    def snapshot(self) -> object:
        return SimpleNamespace(state=self.state, generation=self.generation)

    def tell(self, command: object) -> AdmissionResult:
        self.tell_count += 1
        if isinstance(command, CellActorWakeup):
            self.handler(command)
        return AdmissionResult.ACCEPTED

    def stop(self, _timeout: float = 1.0) -> bool:
        self.stop_count += 1
        self.state = ActorState.STOPPED
        return True


class Host:
    def __init__(self) -> None:
        self.actors: dict[str, Actor] = {}
        self.spawn_count = 0
        self.spawn_gate: threading.Event | None = None
        self.spawn_release: threading.Event | None = None
        self.external: Actor | None = None

    def find(self, _group: str, key: str) -> Actor | None:
        return self.external or self.actors.get(key)

    def spawn(self, spec: object) -> Actor:
        self.spawn_count += 1
        if self.spawn_gate is not None:
            self.spawn_gate.set()
            assert self.spawn_release is not None
            assert self.spawn_release.wait(2.0)
        if spec.key in self.actors:
            raise ActorSpawnRefused("key-in-use")
        actor = Actor(spec, spec.key, self)
        self.actors[spec.key] = actor
        return actor


class CellActorHistoryStoreForTest(CellActorHistoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.next_calls = 0
        self.commit_calls = 0

    def next_epoch(self, cell_id: str) -> str:
        self.next_calls += 1
        return super().next_epoch(cell_id)

    def commit(self, *args: object) -> bool:
        self.commit_calls += 1
        return super().commit(*args)


class SnapshotStoreForTest(CellActorSnapshotStore):
    def __init__(self) -> None:
        super().__init__()
        self.publish_calls = 0

    def publish(self, *args: object) -> bool:
        self.publish_calls += 1
        return super().publish(*args)


def make(cell: object, host: Host | None = None, runtime: Runtime | None = None) -> tuple[CellActorRegistry, Host, Runtime]:
    selected_host = host or Host()
    selected_runtime = runtime or Runtime(cell)
    registry = CellActorRegistry(selected_host, runtime=selected_runtime)
    registry.on_index({cell.cell_id})
    return registry, selected_host, selected_runtime


def test_repeated_on_index_does_not_spawn_again() -> None:
    cell = agent_cell(agent_id())
    registry, host, _runtime = make(cell)
    assert host.spawn_count == 1
    registry.on_index({cell.cell_id})
    assert host.spawn_count == 1
    registry.detach()


def test_factory_does_not_touch_history_or_snapshot_storage() -> None:
    cell = agent_cell(agent_id())
    registry = CellActorRegistry(Host(), runtime=Runtime(cell))
    handler = registry._factory(cell.cell_id)()
    assert handler.core is None
    assert registry.snapshots() == {}


def test_index_removal_stops_actor_and_blocks_late_ensure() -> None:
    cell = agent_cell(agent_id())
    registry, host, _runtime = make(cell)
    actor = host.actors[cell.cell_id]
    registry.on_index(set())
    assert actor.stop_count == 1
    assert registry.ensure_actor(cell.cell_id) is None
    registry.detach()


def test_in_flight_spawn_is_stopped_when_index_removes_cell() -> None:
    cell = agent_cell(agent_id())
    host = Host()
    host.spawn_gate = threading.Event()
    host.spawn_release = threading.Event()
    runtime = Runtime(cell)
    registry = CellActorRegistry(host, runtime=runtime)
    result: list[object] = []
    thread = threading.Thread(
        target=lambda: (registry.on_index({cell.cell_id}), result.append("done"))
    )
    thread.start()
    assert host.spawn_gate.wait(2.0)
    registry.on_index(set())
    host.spawn_release.set()
    thread.join(2.0)
    assert not thread.is_alive()
    assert result == ["done"]
    assert host.actors[cell.cell_id].stop_count == 1
    registry.detach()


def test_stopping_key_requests_retry_and_diagnostic_once() -> None:
    cell = agent_cell(agent_id())
    host = Host()
    existing = Actor.__new__(Actor)
    existing.state = ActorState.STOPPING
    existing.generation = 1
    existing.stop_count = 0
    host.external = existing
    runtime = Runtime(cell)
    registry = CellActorRegistry(host, runtime=runtime)
    registry.on_index({cell.cell_id})
    registry.on_health()
    registry.on_health()
    assert runtime.retries == 3
    registry.detach()


def test_quarantined_actor_is_not_retried_and_snapshot_is_degraded() -> None:
    cell = agent_cell(agent_id())
    log = Log()
    runtime = Runtime(cell)
    host = Host()
    registry = CellActorRegistry(host, runtime=runtime, logger=log)
    registry.on_index({cell.cell_id})
    actor = host.actors[cell.cell_id]
    actor.state = ActorState.QUARANTINED
    registry.on_health()
    registry.on_health()
    assert runtime.retries == 0
    value = registry.snapshot(cell.cell_id)
    assert value is not None
    assert value.state is CellActorState.DEGRADED
    assert value.reason == "actor-quarantined"
    assert log.names().count("cell.actor_quarantined") == 1
    registry.detach()


def test_restart_sink_only_wakes_new_incarnation() -> None:
    cell = agent_cell(agent_id())
    registry, host, _runtime = make(cell)
    actor = host.actors[cell.cell_id]
    before = registry.snapshot(cell.cell_id)
    assert before is not None
    actor.generation = 2
    actor.handler = actor.spec.handler_factory()
    actor.spec.event_sink(ActorEvent(ActorEventKind.CHILD_RESTARTED, actor.handle, 2))
    after = registry.snapshot(cell.cell_id)
    assert after is not None
    assert after.epoch != before.epoch
    registry.detach()


def test_snapshot_second_closed_check_wins() -> None:
    cell = agent_cell(agent_id())
    registry, host, _runtime = make(cell)
    original = registry._snapshots.get

    def close_then_get(cell_id: str) -> object:
        value = original(cell_id)
        registry.detach()
        return value

    registry._snapshots.get = close_then_get
    assert registry.snapshot(cell.cell_id) is None
    assert host.actors[cell.cell_id].stop_count == 1


def test_detach_and_find_interleave_returns_none_after_stopping_actor() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host()
    runtime = Runtime(cell)
    registry = CellActorRegistry(engine, runtime=runtime)
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        entered = threading.Event()
        release = threading.Event()
        result: list[object] = []
        worker: threading.Thread | None = None
        original = registry._mark_active

        def gated(cell_id: str, actor: object, *, clear_reported: bool) -> bool:
            entered.set()
            assert release.wait(5.0)
            return original(cell_id, actor, clear_reported=clear_reported)

        registry._mark_active = gated
        worker = threading.Thread(
            target=lambda: result.append(registry.ensure_actor(cell.cell_id))
        )
        worker.start()
        assert entered.wait(5.0)
        registry.detach()
        release.set()
        worker.join(5.0)
        assert not worker.is_alive()
        assert result == [None]
        assert engine.find(CELL_GROUP, cell.cell_id) is None
    finally:
        release.set()
        if worker is not None:
            worker.join(5.0)
            assert not worker.is_alive()
        registry.detach()
        assert engine.shutdown()


def test_factory_run_then_spawn_failure_leaves_stores_unchanged() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host()
    runtime = Runtime(cell)
    history = CellActorHistoryStoreForTest()
    snapshots = SnapshotStoreForTest()
    registry = CellActorRegistry(
        engine, runtime=runtime, history=history, snapshots=snapshots
    )
    original_factory = registry._factory(cell.cell_id)
    factory_calls: list[int] = []

    def failing_factory(_cell_id: str) -> object:
        def build() -> object:
            factory_calls.append(1)
            original_factory()
            raise RuntimeError("pump start unavailable")

        return build

    registry._factory = failing_factory
    try:
        registry.on_index({cell.cell_id})
        assert factory_calls == [1]
        assert history.next_calls == 0
        assert history.commit_calls == 0
        assert snapshots.publish_calls == 0
        assert history._next == {}
        assert history._committed == {}
        assert history._values == {}
        assert snapshots.snapshots() == {}
    finally:
        registry.detach()
        assert engine.shutdown()


def test_quarantined_reference_actor_is_degraded_once_without_retry() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host(
        ActorEngineConfig(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(0, 60.0, 0.0, 0.0, 0.0)
            }
        )
    )
    runtime = Runtime(cell)
    log = Log()
    registry = CellActorRegistry(
        engine, runtime=runtime, extensions=(PoisonExtension(),), logger=log
    )
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        actor = engine.find(CELL_GROUP, cell.cell_id)
        assert actor is not None
        try:
            actor.ask("poison", timeout=2.0)
        except ActorFailedError:
            pass
        wait_until(lambda: actor.snapshot().state is ActorState.QUARANTINED)
        registry.on_health()
        registry.on_health()
        value = registry.snapshot(cell.cell_id)
        assert value is not None
        assert value.state is CellActorState.DEGRADED
        assert value.reason == "actor-quarantined"
        assert runtime.retries == 0
        assert log.names().count("cell.actor_quarantined") == 1
    finally:
        registry.detach()
        assert engine.shutdown()


def test_index_removal_stops_existing_reference_actor_and_blocks_ensure() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host()
    runtime = Runtime(cell)
    registry = CellActorRegistry(engine, runtime=runtime)
    release = threading.Event()
    worker: threading.Thread | None = None
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        entered = threading.Event()
        original = registry._mark_active

        def gated(cell_id: str, actor: object, *, clear_reported: bool) -> bool:
            entered.set()
            assert release.wait(5.0)
            return original(cell_id, actor, clear_reported=clear_reported)

        registry._mark_active = gated
        result: list[object] = []
        worker = threading.Thread(
            target=lambda: result.append(registry.ensure_actor(cell.cell_id))
        )
        worker.start()
        assert entered.wait(5.0)
        registry.on_index(set())
        release.set()
        worker.join(5.0)
        assert not worker.is_alive()
        assert result == [None]
        assert engine.find(CELL_GROUP, cell.cell_id) is None
        assert registry.ensure_actor(cell.cell_id) is None
        assert engine.find(CELL_GROUP, cell.cell_id) is None
    finally:
        release.set()
        if worker is not None:
            worker.join(5.0)
            assert not worker.is_alive()
        registry.detach()
        assert engine.shutdown()


def test_document_then_health_changes_converge_to_both_latest_values() -> None:
    cell = agent_cell(agent_id())
    updated = agent_cell(cell.binding.value, revision=2)
    engine = actor_host()
    runtime = Runtime(cell)
    registry = CellActorRegistry(engine, runtime=runtime)
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        runtime.cell = updated
        runtime.updated += 1
        registry.on_index({cell.cell_id})
        runtime.stale = True
        runtime.updated += 1
        registry.on_health()
        wait_until(
            lambda: (
                (value := registry.snapshot(cell.cell_id)) is not None
                and value.revision == 2
                and value.source_stale
            )
        )
        value = registry.snapshot(cell.cell_id)
        assert value is not None
        assert value.revision == updated.revision
        assert value.source_stale is True
        assert value.observed_document_digest == document_digest(updated)
        assert value.effective_document_digest == document_digest(updated)
    finally:
        registry.detach()
        assert engine.shutdown()


def test_stopping_key_retries_then_respawns_after_confirmed_stop() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host()
    runtime = Runtime(cell)
    extension = BlockingExtension()
    registry = CellActorRegistry(engine, runtime=runtime, extensions=(extension,))
    entered = threading.Event()
    release = threading.Event()
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        old = engine.find(CELL_GROUP, cell.cell_id)
        assert old is not None
        assert old.tell(BlockCommand(entered, release)) is AdmissionResult.ACCEPTED
        assert entered.wait(5.0)
        assert old.stop(0.01) is False
        wait_until(lambda: old.snapshot().state is ActorState.STOPPING)
        assert registry.ensure_actor(cell.cell_id) is None
        assert runtime.retries == 1
        release.set()
        assert old.stop(2.0)
        wait_until(lambda: engine.find(CELL_GROUP, cell.cell_id) is None)
        registry.on_index({cell.cell_id})
        wait_until(
            lambda: (
                (current := engine.find(CELL_GROUP, cell.cell_id)) is not None
                and current.snapshot().handle.actor_id != old.snapshot().handle.actor_id
            )
        )
        value = registry.snapshot(cell.cell_id)
        assert value is not None
        assert value.sequence == 1
    finally:
        release.set()
        registry.detach()
        assert engine.shutdown()


def test_supervisor_restart_publishes_equivalent_state_in_new_epoch() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host()
    runtime = Runtime(cell)
    registry = CellActorRegistry(engine, runtime=runtime, extensions=(PoisonExtension(),))
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        before = registry.snapshot(cell.cell_id)
        assert before is not None
        actor = engine.find(CELL_GROUP, cell.cell_id)
        assert actor is not None
        try:
            actor.ask("poison", timeout=2.0)
        except ActorFailedError:
            pass
        wait_until(
            lambda: (
                (after := registry.snapshot(cell.cell_id)) is not None
                and after.epoch != before.epoch
                and after.state is before.state
            )
        )
        after = registry.snapshot(cell.cell_id)
        assert after is not None
        assert after.epoch != before.epoch
        assert after.state is before.state
        assert after.revision == before.revision
        assert after.effective_revision == before.effective_revision
        assert after.document_digest == before.document_digest
        assert after.source_stale == before.source_stale
        assert after.reason == before.reason
    finally:
        registry.detach()
        assert engine.shutdown()


def test_index_hook_error_does_not_block_other_cell() -> None:
    first = agent_cell(agent_id("first"))
    second = agent_cell(agent_id("second"))
    engine = actor_host()
    runtime = Runtime(first)
    registry = CellActorRegistry(engine, runtime=runtime)
    original = registry.ensure_actor

    def fail_one(cell_id: str) -> object:
        if cell_id == first.cell_id:
            raise RuntimeError("isolated hook failure")
        return original(cell_id)

    registry.ensure_actor = fail_one
    try:
        registry.on_index({first.cell_id, second.cell_id})
        wait_until(lambda: registry.snapshot(second.cell_id) is not None)
        assert engine.find(CELL_GROUP, second.cell_id) is not None
        assert engine.find(CELL_GROUP, first.cell_id) is None
    finally:
        registry.detach()
        assert engine.shutdown()
