"""The daemon's resident PAC cadence owns clocks independently of actors."""

from __future__ import annotations

import threading
import time
from queue import Empty
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime, GraphClockAuthority, PacActorService
from hyprial.daemon.impl.pac.actors import daemon as pac_actor
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, close_graph, create_graph
from hyprial.daemon.impl.pac.actors.coordinator import (
    ActorCoordinator,
    FileLaunchResolver,
    LaunchSpec,
    RuntimeObservation,
)
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


def _clock_graph(
    store: PacGraphStore,
    state_dir: Path,
    name: str,
    *,
    active: bool = True,
    closed: bool = False,
    with_task: bool = False,
) -> str:
    graph_id = create_graph(store, name=name, created_by="user:hyprial-unit-owner")["graphId"]
    version = 1
    if with_task:
        version = add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id="task",
            owner="user:hyprial-unit-owner",
            brief_ref=f"brief#{name}-task",
            expect_version=version,
        )["version"]
    version = add_node(
        store,
        state_dir,
        graph_id=graph_id,
        node_id="deadline",
        owner="user:hyprial-unit-owner",
        brief_ref=f"brief#{name}-deadline",
        kind="clock",
        deadline_ms=1,
        expect_version=version,
    )["version"]
    if with_task:
        add_edge(
            store,
            graph_id=graph_id,
            from_node="task",
            to_node="deadline",
            expect_version=version,
        )
    if active:
        activate_graph(store, graph_id, actor="user:hyprial-unit-owner")
    if closed:
        close_graph(store, graph_id, actor="user:hyprial-unit-owner")
    return graph_id


def _service(
    state_dir: Path,
    sender: NullSender,
    events: list[tuple[tuple[Any, ...], dict[str, Any]]],
    *,
    runtime: Any | None = None,
    workers: int = 1,
    graph_authority: GraphClockAuthority | None = None,
) -> PacActorService:
    service = PacActorService(
        state_dir=state_dir,
        reference_root=state_dir.parent,
        runtime=runtime or DaemonActorRuntime(SimpleNamespace()),
        sender=sender,
        daemon_epoch="test-epoch",
        logger=lambda *args, **fields: events.append((args, fields)),
        workers=workers,
        graph_authority=graph_authority,
    )
    # These are queue-policy probes, not cadence tests: every submit_tick()
    # forces the actor fallback sweep due so the probe sees one full supply.
    # The event-driven cadence itself is covered by
    # ``tests/daemon/pac/events/test_pac_reconcile_event_driven.py``.
    original_submit = service.submit_tick

    def due_tick() -> None:
        service._actor_fallback_due = 0.0
        original_submit()

    service.submit_tick = due_tick
    if workers == 0:
        # Queue-policy probes inspect one completed scan. Production cadence
        # admission remains asynchronous; join its scanner only in this fixture.
        completed = threading.Event()
        scan = service._scan_and_admit

        def observed_scan(*, force_actors: bool = False) -> None:
            try:
                scan(force_actors=force_actors)
            finally:
                completed.set()

        def settled_tick() -> None:
            completed.clear()
            service._actor_fallback_due = 0.0
            original_submit()
            if not completed.wait(3.0):
                raise TimeoutError("PAC scan fixture did not settle")

        service._scan_and_admit = observed_scan
        service.submit_tick = settled_tick
    return service


class _RecordingActorRuntime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.starts: list[str] = []
        self.stops: list[str] = []

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        self.starts.append(actor_name)
        observation = RuntimeObservation(
            True,
            identity_marker,
            launch.harness,
            operation_id,
        )
        self.running[actor_name] = observation
        return observation

    def stop(
        self,
        actor_name: str,
        launch: LaunchSpec,
        *,
        operation_id: str,
        identity_marker: str,
    ) -> RuntimeObservation:
        self.stops.append(actor_name)
        self.running.pop(actor_name, None)
        return RuntimeObservation(False, operation_id=operation_id)


def _actor_graph(
    store: PacGraphStore,
    state_dir: Path,
    name: str,
    *,
    actor_count: int = 1,
    closed: bool = False,
) -> str:
    graph_id = create_graph(
        store,
        name=name,
        created_by="user:hyprial-unit-owner",
    )["graphId"]
    version = 1
    for index in range(actor_count):
        version = add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id=f"actor-{index:03d}",
            owner="user:hyprial-unit-owner",
            brief_ref=f"brief#{name}-{index:03d}",
            kind="actor",
            actor_name=f"{name}-worker-{index:03d}",
            launch_ref="launch/worker.yaml",
            expect_version=version,
        )["version"]
    activate_graph(store, graph_id, actor="user:hyprial-unit-owner")
    if closed:
        close_graph(store, graph_id, actor="user:hyprial-unit-owner")
    return graph_id


def _seed_pending_activations(store: PacGraphStore, graph_id: str) -> None:
    """Give every actor node a committed in-transition activation.

    The fallback sweep only re-derives a row whose activation is in transition
    (or that has a pending cleanup); a never-activated node is supplied by an
    event instead.  A queue-policy probe that wants the sweep to supply a graph
    must therefore commit that state first.  The activation carries the
    marker ``_prepare_up`` would issue: terminal cleanup treats a marker-less
    activation as proving nothing about the process it observes.
    """

    db = store.write()
    try:
        for node in store.nodes(graph_id):
            if node.kind != "actor":
                continue
            db.execute(
                "INSERT OR IGNORE INTO actor_activations "
                "(graph_id,node_id,incarnation,desired,op,effect_id,"
                "operation_id,identity_marker,updated_at) "
                "VALUES (?,?,0,'up','pending',?,?,?,0)",
                (
                    graph_id,
                    node.node_id,
                    f"effect:{node.node_id}",
                    f"operation:{node.node_id}",
                    f"pac:{graph_id}:{node.node_id}:1:effect-{node.node_id}",
                ),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _close_with_live_unmanaged_actors(
    store: PacGraphStore,
    state_dir: Path,
    graph_id: str,
) -> None:
    runtime = _RecordingActorRuntime()
    coordinator = ActorCoordinator(
        store,
        runtime,
        daemon_epoch="test-epoch",
        resolver=FileLaunchResolver(state_dir.parent),
        sender=NullSender(),
    )
    for node in store.nodes(graph_id):
        if node.kind == "actor":
            coordinator.reconcile(graph_id, node.node_id)
    close_graph(store, graph_id, actor="user:hyprial-unit-owner")


def _drain_actor_queue(service: PacActorService) -> list[tuple[str, str]]:
    jobs: list[tuple[str, str]] = []
    while True:
        try:
            job = service._actor_queue.get_nowait()
        except Empty:
            break
        assert job is not None
        jobs.append(job)
        service._active_actors.discard(job)
    return jobs


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "resident PAC worker did not reach the expected state"


@pytest.fixture()
def pac_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, PacGraphStore]:
    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    home.mkdir()
    state_dir.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HYPRIAL_OWNER", "hyprial-unit-owner")
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "daemon.sock"))
    store = PacGraphStore(default_database_path(state_dir))
    yield state_dir, store
    store.close()


def test_resident_tick_expires_task_clock_graph_without_actor_nodes(pac_state) -> None:
    state_dir, store = pac_state
    graph_id = _clock_graph(store, state_dir, "clock-only", with_task=True)
    assert all(node.kind != "actor" for node in store.nodes(graph_id))
    sender = NullSender()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, sender, events)
    try:
        service.submit_tick()
        _wait_until(lambda: len(sender.sent) == 2)
    finally:
        assert service.close()

    overdue = store.notifications_by_synthetic_event(graph_id)
    assert len(overdue) == 2
    assert {item.recipient for item in overdue} == {"user:hyprial-unit-owner"}
    assert {item.edge for item in overdue} == {
        "clock:deadline",
        "clock-up:task->deadline",
    }
    assert not [item for item in events if item[0][2] == "pac.clock.tick_failed"]


def test_tick_admission_does_not_wait_for_graph_scan(pac_state, monkeypatch) -> None:
    state_dir, _store = pac_state
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, NullSender(), events)
    scanning = threading.Event()
    release = threading.Event()
    original = pac_actor.PacGraphStore

    def slow_store(*args, **kwargs):
        if kwargs.get("read_only"):
            scanning.set()
            assert release.wait(3.0), "test did not release the scan"
        return original(*args, **kwargs)

    monkeypatch.setattr(pac_actor, "PacGraphStore", slow_store)
    try:
        started = time.monotonic()
        service.submit_tick()
        assert time.monotonic() - started < 0.1
        assert scanning.wait(1.0)
        started = time.monotonic()
        for _ in range(100):
            service.submit_tick()
        assert time.monotonic() - started < 0.1
        assert service._scan_queue.qsize() <= 1
    finally:
        release.set()
        assert service.close(3.0)


def test_clock_tick_is_sent_to_graph_writer_when_configured(pac_state) -> None:
    state_dir, store = pac_state
    graph_id = _clock_graph(store, state_dir, "clock-authority")
    called: list[str] = []

    class ClockAuthority:
        def clock_tick(self, target: str) -> dict[str, Any]:
            called.append(target)
            return {"ok": True, "notifications": 0}

        def clock_tick_many(self, targets: Any, **_kwargs: Any) -> dict[str, Any]:
            # One round trip per batch is the point of the dispatch switch:
            # the graph ids arrive together, not one command at a time.
            called.extend(targets)
            return {"ok": True, "graphs": len(targets), "notifications": 0}

    service = _service(state_dir, NullSender(), [], graph_authority=ClockAuthority())
    try:
        service.submit_tick()
        _wait_until(lambda: called == [graph_id])
    finally:
        assert service.close()


def test_the_scan_dispatches_one_batch_instead_of_one_call_per_graph(pac_state) -> None:
    """The dispatch layer must hand the tick over as BATCHES, not per graph.

    The batch command only saves round trips if the service actually uses it:
    before the dispatch switch the scan enqueued one graph per entry and the
    worker paid one `clock_tick` per graph. This pins the service-level shape
    (one `clock_tick_many` carrying every admitted graph, zero single calls), so
    a future change cannot quietly fall back to per-graph dispatch.
    """

    state_dir, store = pac_state
    expected = {_clock_graph(store, state_dir, f"batched-{index}") for index in range(5)}
    batches: list[list[str]] = []
    single_calls: list[str] = []

    class ClockAuthority:
        def clock_tick(self, target: str) -> dict[str, Any]:
            single_calls.append(target)
            return {"ok": True, "notifications": 0}

        def clock_tick_many(self, targets: Any, **_kwargs: Any) -> dict[str, Any]:
            batches.append(list(targets))
            return {"ok": True, "graphs": len(targets), "notifications": 0}

    service = _service(state_dir, NullSender(), [], graph_authority=ClockAuthority())
    try:
        service.submit_tick()
        _wait_until(lambda: sum(len(batch) for batch in batches) == len(expected))
    finally:
        assert service.close()

    assert single_calls == [], f"per-graph dispatch came back: {single_calls}"
    admitted = {graph_id for batch in batches for graph_id in batch}
    assert admitted == expected, sorted(expected - admitted)


def test_resident_tick_does_not_alert_for_inactive_or_closed_graphs(pac_state) -> None:
    state_dir, store = pac_state
    active = _clock_graph(store, state_dir, "a-active")
    inactive = _clock_graph(store, state_dir, "b-inactive", active=False)
    closed = _clock_graph(store, state_dir, "c-closed", closed=True)
    sender = NullSender()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, sender, events)
    try:
        service.submit_tick()
        _wait_until(lambda: len(sender.sent) == 1)
    finally:
        assert service.close()

    assert len(store.notifications_by_synthetic_event(active)) == 1
    assert store.notifications_by_synthetic_event(inactive) == []
    assert store.notifications_by_synthetic_event(closed) == []


def test_resident_tick_is_sql_idempotent_across_repeats_and_restart(pac_state) -> None:
    state_dir, store = pac_state
    graph_id = _clock_graph(store, state_dir, "idempotent")
    sender = NullSender()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    first = _service(state_dir, sender, events)
    try:
        first.submit_tick()
        first.submit_tick()
        _wait_until(lambda: len(sender.sent) == 1)
    finally:
        assert first.close()

    restarted = _service(state_dir, sender, events)
    try:
        restarted.submit_tick()
        restarted.submit_tick()
    finally:
        assert restarted.close()

    assert len(sender.sent) == 1
    assert len(store.notifications_by_synthetic_event(graph_id)) == 1
    assert not [item for item in events if item[0][2] == "pac.clock.tick_failed"]


def test_resident_service_still_admits_actor_reconciliation(
    pac_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, store = pac_state
    graph_id = create_graph(
        store, name="actor-regression", created_by="user:hyprial-unit-owner"
    )["graphId"]
    version = add_node(
        store,
        state_dir,
        graph_id=graph_id,
        node_id="gate",
        owner="user:hyprial-unit-owner",
        brief_ref="brief#gate",
        expect_version=1,
    )["version"]
    version = add_node(
        store,
        state_dir,
        graph_id=graph_id,
        node_id="worker",
        owner="user:hyprial-unit-owner",
        brief_ref="brief#worker",
        kind="actor",
        actor_name="clock-test-worker",
        launch_ref="launch/worker.yaml",
        expect_version=version,
    )["version"]
    add_edge(
        store,
        graph_id=graph_id,
        from_node="gate",
        to_node="worker",
        expect_version=version,
    )
    activate_graph(store, graph_id, actor="user:hyprial-unit-owner")
    _seed_pending_activations(store, graph_id)
    reconciled = threading.Event()
    original = ActorCoordinator.reconcile

    def observe_reconcile(self: ActorCoordinator, candidate_graph: str, node_id: str):
        if candidate_graph == graph_id and node_id == "worker":
            reconciled.set()
        return original(self, candidate_graph, node_id)

    monkeypatch.setattr(ActorCoordinator, "reconcile", observe_reconcile)
    sender = NullSender()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, sender, events)
    try:
        service.submit_tick()
        assert reconciled.wait(3.0), "actor reconciliation was not admitted"
    finally:
        assert service.close()


def test_one_clock_graph_failure_is_logged_and_does_not_block_the_next(
    pac_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, store = pac_state
    broken = _clock_graph(store, state_dir, "a-broken")
    healthy = _clock_graph(store, state_dir, "z-healthy")
    original = PacReactor.tick_clocks

    def fail_one(self: PacReactor, graph_id: str):
        if graph_id == broken:
            raise RuntimeError("injected clock failure")
        return original(self, graph_id)

    monkeypatch.setattr(PacReactor, "tick_clocks", fail_one)
    sender = NullSender()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, sender, events)
    try:
        service.submit_tick()
        _wait_until(lambda: len(sender.sent) == 1)
    finally:
        assert service.close()

    failures = [item for item in events if item[0][2] == "pac.clock.tick_failed"]
    assert len(failures) == 1
    assert failures[0][0][:2] == ("error", "pac")
    assert failures[0][1]["graphId"] == broken
    assert failures[0][1]["errorType"] == "RuntimeError"
    assert failures[0][1]["detail"] == "injected clock failure"
    assert store.notifications_by_synthetic_event(broken) == []
    assert len(store.notifications_by_synthetic_event(healthy)) == 1


def test_clock_queue_backlog_has_a_named_log_event(
    pac_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, store = pac_state
    for index in range(130):
        _clock_graph(store, state_dir, f"clock-{index:03d}")
    entered = threading.Event()
    release = threading.Event()
    original = PacReactor.tick_clocks

    def block_first(self: PacReactor, graph_id: str):
        if not entered.is_set():
            entered.set()
            assert release.wait(3.0), "test did not release the clock worker"
        return original(self, graph_id)

    monkeypatch.setattr(PacReactor, "tick_clocks", block_first)
    sender = NullSender()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, sender, events)
    try:
        service.submit_tick()
        _wait_until(
            lambda: any(item[0][2] == "pac.clock.queue_full" for item in events)
        )
    finally:
        release.set()
        assert service.close()

    backlog = [item for item in events if item[0][2] == "pac.clock.queue_full"]
    assert len(backlog) == 1
    assert backlog[0][0][:2] == ("warn", "pac")
    assert backlog[0][1]["capacity"] == 128
    assert backlog[0][1]["graphId"]


def test_closed_actor_backlog_cannot_starve_an_open_graph(
    pac_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, store = pac_state
    _actor_graph(
        store,
        state_dir,
        "a-closed-backlog",
        actor_count=129,
        closed=True,
    )
    open_graph = _actor_graph(store, state_dir, "z-open")
    _seed_pending_activations(store, open_graph)
    reconciled_open = threading.Event()

    def observe_reconcile(
        self: ActorCoordinator, candidate_graph: str, node_id: str
    ) -> None:
        del self, node_id
        if candidate_graph == open_graph:
            reconciled_open.set()

    monkeypatch.setattr(ActorCoordinator, "reconcile", observe_reconcile)
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    queue_probe = _service(state_dir, NullSender(), events, workers=0)
    try:
        queue_probe.submit_tick()
        assert _drain_actor_queue(queue_probe) == [(open_graph, "actor-000")]
    finally:
        assert queue_probe.close()

    service = _service(state_dir, NullSender(), events)
    try:
        service.submit_tick()
        assert reconciled_open.wait(0.5), (
            "the open graph was left behind the closed-graph actor backlog"
        )
    finally:
        assert service.close()


def test_open_actor_scan_rotates_after_queue_full(pac_state) -> None:
    state_dir, store = pac_state
    graph_id = _actor_graph(store, state_dir, "open-fairness", actor_count=130)
    _seed_pending_activations(store, graph_id)
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, NullSender(), events, workers=0)
    try:
        service.submit_tick()
        first = _drain_actor_queue(service)
        service.submit_tick()
        second = _drain_actor_queue(service)
    finally:
        assert service.close()

    expected = {(graph_id, f"actor-{index:03d}") for index in range(130)}
    assert set(first) | set(second) == expected
    queue_full = [item for item in events if item[0][2] == "pac.actor.queue_full"]
    assert len(queue_full) == 1
    assert queue_full[0][1]["capacity"] == 128
    assert queue_full[0][1]["deferredJobs"] == 2


def test_partial_drain_gives_both_actor_classes_bounded_service(pac_state) -> None:
    state_dir, store = pac_state
    open_graph = _actor_graph(
        store,
        state_dir,
        "partial-drain-open",
        actor_count=129,
    )
    _seed_pending_activations(store, open_graph)
    closed_graph = _actor_graph(
        store,
        state_dir,
        "partial-drain-closed",
        actor_count=129,
    )
    _close_with_live_unmanaged_actors(store, state_dir, closed_graph)
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, NullSender(), events, workers=0)
    admitted: set[tuple[str, str]] = set()
    try:
        for index in range(service._actor_queue.maxsize):
            service._actor_queue.put_nowait((f"prefill-{index:03d}", "actor"))

        for _tick in range(8):
            service._actor_queue.get_nowait()
            assert service._actor_queue.qsize() == service._actor_queue.maxsize - 1
            before = set(service._active_actors)
            service.submit_tick()
            assert service._actor_queue.qsize() == service._actor_queue.maxsize
            newly_admitted = service._active_actors - before
            assert len(newly_admitted) == 1
            admitted.update(newly_admitted)
    finally:
        assert service.close()

    admitted_graphs = {graph_id for graph_id, _node_id in admitted}
    assert open_graph in admitted_graphs, (
        "open actors were starved while the queue drained one slot per tick"
    )
    assert closed_graph in admitted_graphs, (
        "closed cleanup was starved while the queue drained one slot per tick"
    )


def test_closed_graph_with_live_actor_is_still_reclaimed(pac_state) -> None:
    state_dir, store = pac_state
    workflow = GraphWorkflowService(
        state_dir=state_dir,
        owner="hyprial-unit-owner",
        machine="unit-machine",
        sender=NullSender(),
        start_thread=False,
    )
    graph_id = workflow.start(
        yaml_text="""
version: 2
name: live-reclaim
nodes:
  - id: work
    task: work
""",
        sender="user:hyprial-unit-owner",
        operation_key="live-reclaim",
    )["graphId"]
    _seed_pending_activations(store, graph_id)
    runtime = _RecordingActorRuntime()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(
        state_dir,
        NullSender(),
        events,
        runtime=runtime,
    )
    try:
        service.submit_tick()
        _wait_until(lambda: len(runtime.starts) == 1)
        worker_name = runtime.starts[0]
        workflow.cancel(run_id=graph_id, actor="user:hyprial-unit-owner")
        assert workflow.status(run_id=graph_id)["workerCleanup"] == "pending"
        service.submit_tick()
        _wait_until(lambda: runtime.stops == [worker_name])
        assert workflow.status(run_id=graph_id)["workerCleanup"] == "complete"
    finally:
        assert service.close()
        assert workflow.close()

    assert runtime.running == {}
    activation = store._db.execute(
        "SELECT desired,op FROM actor_activations WHERE graph_id=?",
        (graph_id,),
    ).fetchone()
    assert tuple(activation) == ("down", "done")


def test_closed_cleanup_is_admitted_under_continuous_open_backlog(pac_state) -> None:
    state_dir, store = pac_state
    workflow = GraphWorkflowService(
        state_dir=state_dir,
        owner="hyprial-unit-owner",
        machine="unit-machine",
        sender=NullSender(),
        start_thread=False,
    )
    graph_id = workflow.start(
        yaml_text="""
version: 2
name: cleanup-share
nodes:
  - id: work
    task: work
""",
        sender="user:hyprial-unit-owner",
        operation_key="cleanup-share",
    )["graphId"]
    _seed_pending_activations(store, graph_id)
    runtime = _RecordingActorRuntime()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    starter = _service(state_dir, NullSender(), events, runtime=runtime)
    try:
        starter.submit_tick()
        _wait_until(lambda: len(runtime.starts) == 1)
    finally:
        assert starter.close()

    worker_name = runtime.starts[0]
    workflow.cancel(run_id=graph_id, actor="user:hyprial-unit-owner")
    assert workflow.status(run_id=graph_id)["workerCleanup"] == "pending"
    closed_job = next(
        (graph_id, node.node_id)
        for node in store.nodes(graph_id)
        if node.kind == "actor"
    )
    unmanaged_graph = _actor_graph(
        store,
        state_dir,
        "unmanaged-cleanup-share",
    )
    _close_with_live_unmanaged_actors(store, state_dir, unmanaged_graph)
    unmanaged_job = (unmanaged_graph, "actor-000")
    unmanaged_activation = store._db.execute(
        "SELECT desired,op FROM actor_activations WHERE graph_id=? AND node_id=?",
        unmanaged_job,
    ).fetchone()
    assert unmanaged_activation["desired"] == "up"
    assert tuple(unmanaged_activation) != ("down", "done")
    assert (
        store._db.execute(
            "SELECT 1 FROM workflow_worker_receipts WHERE graph_id=? AND actor_node=?",
            unmanaged_job,
        ).fetchone()
        is None
    )
    continuous_open = _actor_graph(store, state_dir, "continuous-open", actor_count=129)
    _seed_pending_activations(store, continuous_open)

    service = _service(state_dir, NullSender(), events, runtime=runtime, workers=0)
    admitted_ticks: dict[tuple[str, str], int] = {}
    expected_closed = {closed_job, unmanaged_job}
    try:
        for tick in range(1, 3):
            service.submit_tick()
            jobs = _drain_actor_queue(service)
            assert len(jobs) == service._actor_queue.maxsize
            for job in expected_closed.intersection(jobs):
                admitted_ticks.setdefault(job, tick)
            if expected_closed.issubset(admitted_ticks):
                coordinator = ActorCoordinator(
                    store,
                    runtime,
                    daemon_epoch="test-epoch",
                    resolver=FileLaunchResolver(state_dir.parent),
                    sender=NullSender(),
                )
                coordinator.reconcile(*closed_job)
                break
    finally:
        assert service.close()
        assert workflow.close()

    assert admitted_ticks == {closed_job: 1, unmanaged_job: 1}, (
        "managed or unmanaged closed cleanup was starved by continuously eligible "
        "open jobs"
    )
    assert runtime.stops == [worker_name]
    assert runtime.running == {}


def test_actor_backlog_is_deferred_not_drained_on_close(
    pac_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close() defers the actor backlog instead of draining it.

    The actor queue is not the system of record: the next daemon epoch's
    fallback sweep re-derives the in-transition actor jobs from the durable
    actor_activations rows, and reconcile() converges those rows
    idempotently.  So once close
    begins, the worker must stop taking jobs -- the behavior asserted here is
    "jobs in the backlog are never reconciled", not any line of the
    implementation.  Remove the worker's _closed guard and this goes red.
    """
    state_dir, store = pac_state
    graph_id = create_graph(
        store, name="actor-backlog", created_by="user:hyprial-unit-owner"
    )["graphId"]
    version = 1
    for index in range(130):
        version = add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id=f"actor-{index:03d}",
            owner="user:hyprial-unit-owner",
            brief_ref=f"brief#actor-{index:03d}",
            kind="actor",
            actor_name=f"backlog-worker-{index:03d}",
            launch_ref="launch/worker.yaml",
            expect_version=version,
        )["version"]
    activate_graph(store, graph_id, actor="user:hyprial-unit-owner")
    _seed_pending_activations(store, graph_id)

    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, str]] = []
    original = ActorCoordinator.reconcile

    def observe_and_block_first(
        self: ActorCoordinator, candidate_graph: str, node_id: str
    ):
        calls.append((candidate_graph, node_id))
        if not entered.is_set():
            entered.set()
            assert release.wait(3.0), "test did not release the actor worker"
        return original(self, candidate_graph, node_id)

    monkeypatch.setattr(ActorCoordinator, "reconcile", observe_and_block_first)
    sender = NullSender()
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    service = _service(state_dir, sender, events)
    closed: list[bool] = []
    closer = threading.Thread(target=lambda: closed.append(service.close()))
    try:
        service.submit_tick()
        assert entered.wait(3.0), "actor worker never took the first job"
        # Begin close() while the first job is still blocked.  `_closed` is
        # only the synchronization point (close() sets it first, before it
        # blocks on the full queue's sentinel slot); the assertion below is
        # on the observable behavior -- which jobs got reconciled.
        closer.start()
        _wait_until(lambda: service._closed)
        release.set()
        closer.join(5.0)
    finally:
        release.set()
        closer.join(5.0)
        assert service.close()

    assert closed == [True], "close() did not finish within budget"
    assert len(calls) == 1, (
        "actor worker kept reconciling backlog jobs after close() began: "
        f"{len(calls)} reconcile calls, expected exactly 1 (the in-flight job)"
    )

    # The other half of the declared semantics -- "deferred, not lost": the
    # next service instance re-derives every actor job from the nodes table
    # and converges the jobs the closed instance dropped.  128 of the 130
    # nodes fit the queue (the clock backlog test above pins the queue_full
    # drop for the rest), so all 128 admitted jobs must reconcile here.
    restarted = _service(state_dir, sender, events)
    try:
        restarted.submit_tick()
        _wait_until(lambda: len(set(calls)) == 128, timeout=10.0)
    finally:
        assert restarted.close()


def test_clock_scan_rotates_after_capacity_is_drained(
    pac_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, store = pac_state
    expected = {
        _clock_graph(store, state_dir, f"fair-clock-{index:03d}")
        for index in range(130)
    }
    release = threading.Event()
    monkeypatch.setattr(PacActorService, "_clock_worker", lambda _self: release.wait(10))
    service = _service(state_dir, NullSender(), [])
    admitted: set[str] = set()
    try:
        # Pause native clock execution; drain admission deterministically
        # between cadences without changing the scanner or its real queue.
        for _ in range(3):
            service._scan_and_admit()
            while not service._clock_queue.empty():
                batch = service._clock_queue.get_nowait()
                assert batch is not None
                admitted.update(batch)
                with service._lock:
                    service._active_clocks.difference_update(batch)
        assert admitted == expected, f"Never admitted: {sorted(expected - admitted)}"
    finally:
        release.set()
        assert service.close()
