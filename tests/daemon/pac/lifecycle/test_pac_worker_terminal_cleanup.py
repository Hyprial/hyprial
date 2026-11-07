"""Phase 3 terminal cleanup is receipt-owned, durable, and fail closed."""

from __future__ import annotations

import sqlite3
import pytest

from hyprial.daemon.impl.pac.actors.coordinator import (
    CLEANUP_ATTEMPT_LIMIT,
    ActorCoordinator,
    FileLaunchResolver,
    RuntimeObservation,
)
from hyprial.daemon.impl.pac.graphs.edits import close_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


USER = "user:test"
BORROWED = "agent:test:machine:wf-borrowed-adversary"


class Runtime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.starts: list[tuple[str, str]] = []
        self.stops: list[tuple[str, str]] = []
        self.sticky = False
        self.stop_error: Exception | None = None

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(self, actor_name, launch, *, operation_id, identity_marker):
        self.starts.append((actor_name, operation_id))
        observation = RuntimeObservation(
            True,
            identity_marker,
            launch.harness,
            operation_id,
            agent_entity_token=f"entity-{actor_name}",
        )
        self.running[actor_name] = observation
        return observation

    def stop(self, actor_name, launch, *, operation_id, identity_marker):
        self.stops.append((actor_name, operation_id))
        if self.stop_error is not None:
            raise self.stop_error
        if self.sticky:
            return self.running[actor_name]
        self.running.pop(actor_name, None)
        return RuntimeObservation(False, operation_id=operation_id)


def _service(tmp_path, clock: list[int]):
    return GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=NullSender(),
        clock_ms=lambda: clock[0],
        start_thread=False,
    )


def _coordinator(
    service: GraphWorkflowService,
    store: PacGraphStore,
    runtime: Runtime,
    tmp_path,
    clock: list[int],
    *,
    epoch: str = "epoch-1",
) -> ActorCoordinator:
    return ActorCoordinator(
        store,
        runtime,
        daemon_epoch=epoch,
        resolver=FileLaunchResolver(tmp_path),
        sender=NullSender(),
        clock=lambda: clock[0],
    )


def _complete_requested_nodes(
    service: GraphWorkflowService, store: PacGraphStore, graph_id: str
) -> None:
    service._tick()
    for node in service.status(run_id=graph_id)["nodes"]:
        assert node["requestId"] is not None
        PacReactor(store, clock=service.clock).set_flag(
            graph_id,
            node["nodeId"],
            actor=node["owner"],
            expected_request=node["requestId"],
            reason_ref=f"result:{node['nodeId']}",
        )
    service._tick()


def _assert_worker_reclaimed_once(
    store: PacGraphStore,
    runtime: Runtime,
    graph_id: str,
) -> None:
    [receipt] = store.workflow_worker_receipts(graph_id)
    [intent] = store.workflow_worker_cleanup_intents(graph_id)
    activation = store._db.execute(
        "SELECT desired,op FROM actor_activations WHERE graph_id=? AND node_id=?",
        (graph_id, receipt["actor_node"]),
    ).fetchone()

    assert tuple(activation) == ("down", "done")
    assert receipt["state"] == "down"
    assert intent["state"] == "complete"
    assert runtime.running == {}
    assert [name for name, _operation in runtime.stops] == [receipt["actor_name"]]


def test_end_flag_reclaims_receipt_owned_worker_exactly_once(tmp_path) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: end-flag-cleanup
nodes:
  - id: work
    task: work
  - id: finish
    kind: end
    owner: user:test
    task: finish
    after: [work]
""",
            sender=USER,
            operation_key="end-flag-cleanup",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        coordinator = _coordinator(service, store, runtime, tmp_path, clock)
        try:
            coordinator.reconcile_all()
            assert len(runtime.running) == 1

            service._tick()
            work = next(
                node
                for node in service.status(run_id=graph_id)["nodes"]
                if node["nodeId"] == "work"
            )
            PacReactor(store, clock=service.clock).set_flag(
                graph_id,
                "work",
                actor=work["owner"],
                expected_request=work["requestId"],
                reason_ref="result:work",
            )
            service._tick()
            finish = next(
                node
                for node in service.status(run_id=graph_id)["nodes"]
                if node["nodeId"] == "finish"
            )
            PacReactor(store, clock=service.clock).set_flag(
                graph_id,
                "finish",
                actor=finish["owner"],
                expected_request=finish["requestId"],
                reason_ref="result:finish",
                output_text="accepted output",
            )

            assert service.status(run_id=graph_id)["state"] == "completed"
            graph = store.graph(graph_id)
            assert graph is not None and graph["closed_by"] == "finish"
            workflow = store._db.execute(
                "SELECT state,reason_ref FROM workflow_graphs WHERE graph_id=?",
                (graph_id,),
            ).fetchone()
            assert tuple(workflow) == ("completed", "result:finish")
            finish_row = store._db.execute(
                "SELECT state,reason_ref,output_text FROM workflow_nodes "
                "WHERE graph_id=? AND node_id='finish'",
                (graph_id,),
            ).fetchone()
            assert tuple(finish_row) == (
                "done",
                "result:finish",
                "accepted output",
            )
            coordinator.reconcile_all()
            _assert_worker_reclaimed_once(store, runtime, graph_id)
            coordinator.reconcile_all()
            assert len(runtime.stops) == 1
        finally:
            store.close()
    finally:
        assert service.close()


def test_close_graph_reclaims_managed_receipt_owned_worker_exactly_once(
    tmp_path,
) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: managed-close-cleanup
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="managed-close-cleanup",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        coordinator = _coordinator(service, store, runtime, tmp_path, clock)
        try:
            coordinator.reconcile_all()
            assert len(runtime.running) == 1

            close_graph(store, graph_id, actor=USER)

            assert service.status(run_id=graph_id)["state"] == "cancelled"
            workflow = store._db.execute(
                "SELECT state,reason_ref FROM workflow_graphs WHERE graph_id=?",
                (graph_id,),
            ).fetchone()
            assert tuple(workflow) == ("cancelled", "pac:graph-closed")
            coordinator.reconcile_all()
            _assert_worker_reclaimed_once(store, runtime, graph_id)
            coordinator.reconcile_all()
            assert len(runtime.stops) == 1
        finally:
            store.close()
    finally:
        assert service.close()


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled", "expired"])
def test_every_terminal_path_stops_all_receipt_owned_workers(
    tmp_path, terminal: str
) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: terminal-paths
nodes:
  - id: first
    task: first
    deadline_ms: 1500
  - id: second
    task: second
    deadline_ms: 1500
""",
            sender=USER,
            operation_key=f"terminal-{terminal}",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        coordinator = _coordinator(service, store, runtime, tmp_path, clock)
        try:
            coordinator.reconcile_all()
            assert len(runtime.running) == 2
            service._tick()
            if terminal == "completed":
                _complete_requested_nodes(service, store, graph_id)
            elif terminal == "failed":
                node = service.status(run_id=graph_id)["nodes"][0]
                service.fail(
                    graph_id=graph_id,
                    node_id=node["nodeId"],
                    actor=node["owner"],
                    request_id=node["requestId"],
                    reason_ref="test:failed",
                )
            elif terminal == "cancelled":
                service.cancel(run_id=graph_id, actor=USER)
            else:
                clock[0] = 1501
                service._tick()

            pending = service.status(run_id=graph_id)
            assert pending["state"] == terminal.replace("expired", "failed")
            assert pending["workerCleanup"] == "pending"
            assert len(store.workflow_worker_cleanup_intents(graph_id)) == 2

            coordinator.reconcile_all()

            assert runtime.running == {}
            assert {name for name, _operation in runtime.stops} == {
                receipt["actor_name"]
                for receipt in store.workflow_worker_receipts(graph_id)
            }
            assert service.status(run_id=graph_id)["workerCleanup"] == "complete"
            assert {
                receipt["state"]
                for receipt in store.workflow_worker_receipts(graph_id)
            } == {"down"}
        finally:
            store.close()
    finally:
        assert service.close()


def test_hold_does_not_create_cleanup_or_stop_worker(tmp_path) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: held
on_failure: hold
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="held",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        coordinator = _coordinator(service, store, runtime, tmp_path, clock)
        try:
            coordinator.reconcile_all()
            service._tick()
            node = service.status(run_id=graph_id)["nodes"][0]
            held = service.fail(
                graph_id=graph_id,
                node_id=node["nodeId"],
                actor=node["owner"],
                request_id=node["requestId"],
                reason_ref="test:held",
            )
            coordinator.reconcile_all()

            assert held["state"] == "held"
            assert "workerCleanup" not in held
            assert store.workflow_worker_cleanup_intents(graph_id) == []
            assert runtime.running and runtime.stops == []
        finally:
            store.close()
    finally:
        assert service.close()


def test_never_started_owned_worker_is_confirmed_absent_and_marked_down(
    tmp_path,
) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: never-started
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="never-started",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        try:
            service.cancel(run_id=graph_id, actor=USER)
            _coordinator(service, store, runtime, tmp_path, clock).reconcile_all()

            activation = store._db.execute(
                "SELECT desired,op,identity_marker FROM actor_activations "
                "WHERE graph_id=?",
                (graph_id,),
            ).fetchone()
            assert tuple(activation) == ("down", "done", None)
            assert service.status(run_id=graph_id)["workerCleanup"] == "complete"
            assert store.workflow_worker_receipts(graph_id)[0]["state"] == "down"
            assert runtime.starts == [] and runtime.stops == []
        finally:
            store.close()
    finally:
        assert service.close()


def test_borrowed_prefix_actor_has_no_cleanup_authority(tmp_path) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text=f"""
version: 2
name: borrowed-prefix
nodes:
  - id: borrowed
    owner: {BORROWED}
    task: borrowed
""",
            sender=USER,
            operation_key="borrowed-prefix",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        runtime.running["wf-borrowed-adversary"] = RuntimeObservation(
            True, "borrowed-marker", "codex"
        )
        try:
            status = service.cancel(run_id=graph_id, actor=USER)

            assert status["workerCleanup"] == "complete"
            assert store.workflow_worker_receipts(graph_id) == []
            assert store.workflow_worker_cleanup_intents(graph_id) == []
            assert runtime.observe("wf-borrowed-adversary").present
            assert runtime.stops == []
        finally:
            store.close()
    finally:
        assert service.close()


def test_cleanup_intent_identity_is_immutable(tmp_path) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: immutable-cleanup
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="immutable-cleanup",
        )["graphId"]
        service.cancel(run_id=graph_id, actor=USER)
        store = PacGraphStore(service.database)
        try:
            db = store.write()
            with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
                db.execute(
                    "UPDATE workflow_worker_cleanup_intents "
                    "SET operation_id='replacement' WHERE graph_id=?",
                    (graph_id,),
                )
            db.rollback()
        finally:
            store.close()
    finally:
        assert service.close()


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("marker", "marker_mismatch"), ("timeout", "stop_timeout")],
)
def test_unsafe_process_observation_moves_cleanup_to_attention(
    tmp_path, mode: str, reason: str
) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: cleanup-attention
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key=f"attention-{mode}",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        coordinator = _coordinator(service, store, runtime, tmp_path, clock)
        try:
            coordinator.reconcile_all()
            service.cancel(run_id=graph_id, actor=USER)
            if mode == "marker":
                actor_name = store.workflow_worker_receipts(graph_id)[0]["actor_name"]
                runtime.running[actor_name] = RuntimeObservation(
                    True,
                    "pac:another-process",
                    "codex",
                    agent_entity_token=f"entity-{actor_name}",
                )
            else:
                runtime.stop_error = TimeoutError("stop did not settle")

            clock[0] = 1300
            coordinator.reconcile_all()

            [intent] = store.workflow_worker_cleanup_intents(graph_id)
            assert intent["state"] == "attention"
            assert intent["attention_reason"] == reason
            assert service.status(run_id=graph_id)["workerCleanup"] == "attention"
            assert store.workflow_worker_receipts(graph_id)[0]["state"] == "planned"
            assert runtime.running
        finally:
            store.close()
    finally:
        assert service.close()


def test_still_present_stop_is_bounded_before_attention(tmp_path) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: bounded-cleanup
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="bounded-cleanup",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        runtime.sticky = True
        coordinator = _coordinator(service, store, runtime, tmp_path, clock)
        try:
            coordinator.reconcile_all()
            service.cancel(run_id=graph_id, actor=USER)
            for attempt in range(CLEANUP_ATTEMPT_LIMIT):
                clock[0] += 100
                coordinator.reconcile_all()
                state = store.workflow_worker_cleanup_intents(graph_id)[0]["state"]
                assert state == (
                    "attention"
                    if attempt + 1 == CLEANUP_ATTEMPT_LIMIT
                    else "pending"
                )
            assert len(runtime.stops) == CLEANUP_ATTEMPT_LIMIT
        finally:
            store.close()
    finally:
        assert service.close()


def test_restart_after_stop_effect_converges_without_second_stop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000]
    service = _service(tmp_path, clock)
    try:
        graph_id = service.start(
            yaml_text="""
version: 2
name: cleanup-restart
nodes:
  - id: work
    task: work
""",
            sender=USER,
            operation_key="cleanup-restart",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = Runtime()
        first = _coordinator(service, store, runtime, tmp_path, clock)
        try:
            first.reconcile_all()
            service.cancel(run_id=graph_id, actor=USER)
            [before] = store.workflow_worker_cleanup_intents(graph_id)

            monkeypatch.setattr(
                first,
                "_complete_down",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("injected after stop effect")
                ),
            )
            with pytest.raises(RuntimeError, match="injected after stop effect"):
                first.reconcile_all()
            assert len(runtime.stops) == 1
            assert store.workflow_worker_cleanup_intents(graph_id)[0]["state"] == "pending"

            clock[0] = 1400
            restarted = _coordinator(
                service, store, runtime, tmp_path, clock, epoch="epoch-2"
            )
            restarted.reconcile_all()

            [after] = store.workflow_worker_cleanup_intents(graph_id)
            assert after["operation_id"] == before["operation_id"]
            assert after["state"] == "complete"
            assert len(runtime.stops) == 1
            completed = [
                event
                for event in store.journal_events(graph_id)
                if event["type"] == "workflow_changed"
                and event["data"].get("workerCleanup") == "complete"
            ]
            assert len(completed) == 1
        finally:
            store.close()
    finally:
        assert service.close()
