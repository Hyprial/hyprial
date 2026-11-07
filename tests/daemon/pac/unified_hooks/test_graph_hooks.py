"""H5 emit points: ``graph.closed`` and ``worker.cleanup.complete``.

Design ``design-unified-hooks-2026-10-07.md`` §2.2/§4 (contract fixed
2026-10-08, commit 5201b585d): the payload contract is checked, each fact is
emitted exactly once at the commit boundary, an unmanaged end-flag close
carries ``endState: null``, and an absent publisher is a no-op.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.actors.coordinator import (
    ActorCoordinator,
    FileLaunchResolver,
    RuntimeObservation,
)
from hyprial.daemon.impl.pac.contracts.expansion.types import PreparedExpansion
from hyprial.daemon.impl.pac.contracts.workflow import WorkflowNode, WorkflowSpec
from hyprial.daemon.impl.pac.graphs.edits import (
    activate_graph,
    add_edge,
    add_node,
    close_graph,
    create_graph,
)
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.expansion.transactions import child_graph_id
from hyprial.daemon.impl.pac.workflows.graphs import (
    compile_workflow,
    input_token,
    prepare_workflow_artifacts,
)
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.kernel import HookBus, HookEvent, HookEventPublisher

USER = "user:test"
AGENT = "agent:test:machine:worker"
OWNER = "user:hyprial-unit-owner"


class _Capture:
    def __init__(self) -> None:
        self.events: list[HookEvent] = []
        self._lock = threading.Lock()

    def observe(self, event: HookEvent) -> None:
        with self._lock:
            self.events.append(event)

    def snapshot(self) -> list[HookEvent]:
        with self._lock:
            return list(self.events)

    def wait_for(self, count: int, timeout: float = 2.0) -> list[HookEvent]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.snapshot()
            if len(events) >= count:
                return events
            time.sleep(0.005)
        raise AssertionError(
            f"expected {count} events, got {len(self.snapshot())}"
        )


class _Runtime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.stops: list[tuple[str, str]] = []

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(self, actor_name, launch, *, operation_id, identity_marker):
        observation = RuntimeObservation(True, identity_marker, launch.harness)
        self.running[actor_name] = observation
        return observation

    def stop(self, actor_name, launch, *, operation_id, identity_marker):
        self.stops.append((actor_name, operation_id))
        self.running.pop(actor_name, None)
        return RuntimeObservation(False, operation_id=operation_id)


@pytest.fixture()
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    return state


def _bus_capture(events: list[str]) -> tuple[HookBus, _Capture]:
    bus = HookBus()
    capture = _Capture()
    bus.register("h5-capture", capture, events)
    return bus, capture


def _service(tmp_path: Path, clock: list[int], publisher=None) -> GraphWorkflowService:
    return GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=NullSender(),
        clock_ms=lambda: clock[0],
        start_thread=False,
        hook_publisher=publisher,
    )


END_FLAG_WORKFLOW = """
version: 2
name: end-flag-close
nodes:
  - id: work
    task: work
  - id: finish
    kind: end
    owner: user:test
    task: finish
    after: [work]
"""

DEADLINE_WORKFLOW = """
version: 2
name: h5-deadline
on_failure: terminate
nodes:
  - id: a
    task: a
    owner: user:test
    timeout: 1s
"""


def _status_node(service: GraphWorkflowService, graph_id: str, node_id: str):
    return next(
        node
        for node in service.status(run_id=graph_id)["nodes"]
        if node["nodeId"] == node_id
    )


def _start_worker(
    store: PacGraphStore, tmp_path: Path, clock: list[int], publisher=None
) -> ActorCoordinator:
    """Bring the workflow's worker up so ``_tick`` can mint its request."""

    coordinator = ActorCoordinator(
        store,
        _Runtime(),
        daemon_epoch="epoch-1",
        resolver=FileLaunchResolver(tmp_path),
        sender=NullSender(),
        clock=lambda: clock[0],
        publisher=publisher,
    )
    coordinator.reconcile_all()
    return coordinator


def test_managed_end_flag_emits_graph_closed_exactly_once(tmp_path: Path) -> None:
    clock = [1_000]
    bus, capture = _bus_capture(["graph.closed"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    service = _service(tmp_path, clock, publisher)
    store: PacGraphStore | None = None
    try:
        graph_id = service.start(
            yaml_text=END_FLAG_WORKFLOW,
            sender=USER,
            operation_key="h5-end-flag",
        )["graphId"]
        store = PacGraphStore(service.database)
        _start_worker(store, tmp_path, clock)
        service._tick()
        work = _status_node(service, graph_id, "work")
        PacReactor(store, clock=service.clock).set_flag(
            graph_id,
            "work",
            actor=work["owner"],
            expected_request=work["requestId"],
            reason_ref="result:work",
        )
        service._tick()
        finish = _status_node(service, graph_id, "finish")
        PacReactor(store, clock=service.clock, publisher=publisher).set_flag(
            graph_id,
            "finish",
            actor=finish["owner"],
            expected_request=finish["requestId"],
            reason_ref="result:finish",
            output_text="accepted output",
        )

        [event] = capture.wait_for(1)
        assert event.event == "graph.closed"
        assert event.graph_id == graph_id
        assert dict(event.payload) == {
            "graphId": graph_id,
            "endState": "completed",
            "closedBy": "finish",
            "reasonRef": "result:finish",
            "closedAtMs": 1_000,
            "collectionMethod": "daemon",
            "fidelity": "observed",
        }
        # A later tick must not re-emit the same graph.
        service._tick()
        time.sleep(0.05)
        assert len(capture.snapshot()) == 1
    finally:
        if store is not None:
            store.close()
        assert bus.close(5.0)
        assert service.close()


def test_unmanaged_end_flag_closes_with_null_end_state(state_dir: Path) -> None:
    bus, capture = _bus_capture(["graph.closed"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="unmanaged", created_by=OWNER)["graphId"]
        version = 1
        for node_id in ("root", "end"):
            version = add_node(
                store,
                state_dir,
                graph_id=graph_id,
                node_id=node_id,
                owner=OWNER,
                brief_ref=f"ref#{node_id}",
                kind="end" if node_id == "end" else "task",
                expect_version=version,
            )["version"]
        add_edge(
            store,
            graph_id=graph_id,
            from_node="root",
            to_node="end",
            expect_version=version,
        )
        activate_graph(store, graph_id, actor=OWNER)

        PacReactor(store, clock=lambda: 777, publisher=publisher).set_flag(
            graph_id, "end", actor=OWNER, reason_ref="done"
        )

        [event] = capture.wait_for(1)
        assert dict(event.payload) == {
            "graphId": graph_id,
            "endState": None,
            "closedBy": "end",
            "reasonRef": "done",
            "closedAtMs": 777,
            "collectionMethod": "daemon",
            "fidelity": "observed",
        }
        graph = store.graph(graph_id)
        assert graph is not None and graph["closed_by"] == "end"
        assert [
            entry["type"] for entry in store.journal_events(graph_id)
        ].count("graph_closed") == 1
    finally:
        store.close()
        assert bus.close(5.0)


def test_deadline_expired_emits_graph_closed_exactly_once(tmp_path: Path) -> None:
    """A node deadline that terminates the graph is a transition to closed."""

    clock = [1_000]
    bus, capture = _bus_capture(["graph.closed"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    service = _service(tmp_path, clock, publisher)
    try:
        graph_id = service.start(
            yaml_text=DEADLINE_WORKFLOW,
            sender=USER,
            operation_key="h5-deadline",
        )["graphId"]
        service._tick()
        clock[0] = 2_001
        service._tick()

        assert service.status(run_id=graph_id)["state"] == "failed"
        [event] = capture.wait_for(1)
        payload = dict(event.payload)
        assert payload["endState"] == "failed"
        assert payload["closedBy"] == USER
        assert payload["reasonRef"] == "pac:node-failed:a"
        assert payload["collectionMethod"] == "daemon"
        assert payload["fidelity"] == "observed"

        clock[0] = 999_999
        service._tick()
        time.sleep(0.05)
        assert len(capture.snapshot()) == 1
    finally:
        assert bus.close(5.0)
        assert service.close()


def test_node_failed_terminate_emits_graph_closed_exactly_once(
    tmp_path: Path,
) -> None:
    """An explicit node failure under on_failure=terminate closes the graph."""

    clock = [1_000]
    bus, capture = _bus_capture(["graph.closed"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    service = _service(tmp_path, clock, publisher)
    try:
        graph_id = service.start(
            yaml_text=DEADLINE_WORKFLOW,
            sender=USER,
            operation_key="h5-node-failed",
        )["graphId"]
        service._tick()
        node = _status_node(service, graph_id, "a")
        service.fail(
            graph_id=graph_id,
            node_id="a",
            actor=node["owner"],
            request_id=node["requestId"],
            reason_ref="result:bad",
        )

        assert service.status(run_id=graph_id)["state"] == "failed"
        [event] = capture.wait_for(1)
        payload = dict(event.payload)
        assert payload["endState"] == "failed"
        assert payload["closedBy"] == USER
        assert payload["reasonRef"] == "pac:node-failed:a"

        # The accepted failure receipt makes a replay a no-op, not a re-emit.
        service.fail(
            graph_id=graph_id,
            node_id="a",
            actor=node["owner"],
            request_id=node["requestId"],
            reason_ref="result:bad",
        )
        time.sleep(0.05)
        assert len(capture.snapshot()) == 1
    finally:
        assert bus.close(5.0)
        assert service.close()


def test_unmanaged_owner_close_emits_graph_closed_exactly_once(
    state_dir: Path,
) -> None:
    """edits.close_graph on an unmanaged graph is a transition to closed."""

    bus, capture = _bus_capture(["graph.closed"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    store = PacGraphStore(default_database_path(state_dir))
    try:
        graph_id = create_graph(store, name="owner-close", created_by=OWNER)[
            "graphId"
        ]
        close_graph(store, graph_id, actor=OWNER, publisher=publisher)

        [event] = capture.wait_for(1)
        payload = dict(event.payload)
        assert payload["graphId"] == graph_id
        assert payload["endState"] is None
        assert payload["closedBy"] == OWNER
        assert payload["reasonRef"] is None
        assert isinstance(payload["closedAtMs"], int)
        assert payload["collectionMethod"] == "daemon"
        assert payload["fidelity"] == "observed"

        close_graph(store, graph_id, actor=OWNER, publisher=publisher)
        time.sleep(0.05)
        assert len(capture.snapshot()) == 1
    finally:
        store.close()
        assert bus.close(5.0)


def _node(
    node_id: str,
    *,
    kind: str = "task",
    owner: str | None = AGENT,
    role: str = "execute",
    after: tuple[str, ...] = (),
) -> tuple[WorkflowNode, tuple[tuple[str, str, str], ...]]:
    return (
        WorkflowNode(
            id=node_id,
            kind=kind,
            task=f"do {node_id}",
            owner=owner,
            worker=None,
            launch=None,
            timeout_ms=60_000,
            deadline_ms=None,
            role=role,
            first_output_eta=None,
            human_gates=None,
        ),
        tuple((source, node_id, "forward") for source in after),
    )


def _spec(
    name: str, *values: tuple[WorkflowNode, tuple[tuple[str, str, str], ...]]
) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        summary="",
        nodes=tuple(value[0] for value in values),
        edges=tuple(edge for value in values for edge in value[1]),
        on_failure="terminate",
        escalate_to=None,
    )


def _parent_with_open_child(
    store: PacGraphStore, publisher: HookEventPublisher
) -> tuple[str, str]:
    """Compile a parent, publish one expansion, and return (parent, child)."""

    planner = replace(_node("planner", role="plan")[0], expands="slot")
    placeholder, edges = _node(
        "slot", kind="expansion", owner=None, role="plan", after=("planner",)
    )
    parent_id = compile_workflow(
        store,
        _spec("parent", (planner, ()), (placeholder, edges)),
        sender=USER,
        machine="machine",
        local_owner="test",
        operation_key="cascade-parent",
        at=100,
    )
    token = input_token(store, parent_id, "planner")
    assert token is not None
    with store.write() as db:
        db.execute(
            "UPDATE workflow_nodes SET state='requested',request_id='request-1',"
            "input_token=?,generation=1,deadline_ms=1000 "
            "WHERE graph_id=? AND node_id='planner'",
            (token, parent_id),
        )
    child = _spec("child", _node("work"))
    context = PreparedExpansion(
        parent_graph_id=parent_id,
        planner_node_id="planner",
        planner_request_id="request-1",
        placeholder_node_id="slot",
        child_graph_id=child_graph_id(parent_id, "slot"),
        expansion_digest="d" * 64,
        captured_input_token=token,
        effective_limits={"maxNodes": 1},
        policy_snapshot={"version": 1},
        spec=child,
        prepared_artifacts=prepare_workflow_artifacts(store, child),
        worktrees=(),
    )
    PacReactor(
        store, clock=lambda: 200, expansion_context=context, publisher=publisher
    ).set_flag(
        parent_id,
        "planner",
        actor=AGENT,
        expected_request="request-1",
        reason_ref="evidence:plan",
        output_text="planned",
        expansion="version: 2",
    )
    assert store.graph(context.child_graph_id) is not None
    return parent_id, context.child_graph_id


@pytest.mark.parametrize(
    ("trigger", "cascade_reason"),
    (
        ("cancel", "user:cancelled"),
        ("managed_owner_close", "pac:graph-closed"),
    ),
)
def test_parent_close_cascades_one_graph_closed_per_open_child(
    tmp_path: Path, trigger: str, cascade_reason: str
) -> None:
    """Every managed close cascades: one graph.closed per open child graph."""

    clock = [1_000]
    bus, capture = _bus_capture(["graph.closed"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    service = _service(tmp_path, clock, publisher)
    store = PacGraphStore(service.database)
    try:
        parent_id, child_id = _parent_with_open_child(store, publisher)

        if trigger == "cancel":
            service.cancel(
                run_id=parent_id, actor=USER, reason_ref=cascade_reason
            )
        else:
            close_graph(store, parent_id, actor=USER, publisher=publisher)

        events = capture.wait_for(2)
        child_events = [
            dict(event.payload)
            for event in events
            if event.payload["graphId"] == child_id
        ]
        assert len(child_events) == 1, "each open child gets exactly one event"
        assert child_events[0]["endState"] == "cancelled"
        assert child_events[0]["closedBy"] == parent_id
        assert child_events[0]["reasonRef"] == cascade_reason
        assert child_events[0]["collectionMethod"] == "daemon"
        assert child_events[0]["fidelity"] == "observed"

        child = store.graph(child_id)
        assert child is not None and child["closed_at"] is not None
        assert child["closed_by"] == parent_id
        binding = store._db.execute(
            "SELECT state FROM workflow_graphs WHERE graph_id=?", (child_id,)
        ).fetchone()
        assert binding["state"] == "cancelled"

        # Closing the now-closed parent again must not re-cascade the child.
        if trigger == "cancel":
            service.cancel(run_id=parent_id, actor=USER, reason_ref=cascade_reason)
        else:
            close_graph(store, parent_id, actor=USER, publisher=publisher)
        time.sleep(0.05)
        assert len(
            [
                event
                for event in capture.snapshot()
                if event.payload["graphId"] == child_id
            ]
        ) == 1
    finally:
        store.close()
        assert bus.close(5.0)
        assert service.close()


def test_cancel_emits_graph_closed_exactly_once(tmp_path: Path) -> None:
    clock = [2_000]
    bus, capture = _bus_capture(["graph.closed"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    service = _service(tmp_path, clock, publisher)
    try:
        graph_id = service.start(
            yaml_text=END_FLAG_WORKFLOW,
            sender=USER,
            operation_key="h5-cancel",
        )["graphId"]
        service.cancel(run_id=graph_id, actor=USER, reason_ref="user:cancelled")

        [event] = capture.wait_for(1)
        payload = dict(event.payload)
        assert payload["endState"] == "cancelled"
        assert payload["closedBy"] == USER
        assert payload["reasonRef"] == "user:cancelled"
        assert payload["closedAtMs"] == 2_000
        assert payload["collectionMethod"] == "daemon"
        assert payload["fidelity"] == "observed"
        # Idempotent cancel does not emit a second event.
        service.cancel(run_id=graph_id, actor=USER, reason_ref="user:cancelled")
        assert len(capture.snapshot()) == 1
    finally:
        assert bus.close(5.0)
        assert service.close()


def test_worker_cleanup_complete_emitted_exactly_once(tmp_path: Path) -> None:
    clock = [3_000]
    bus, capture = _bus_capture(["worker.cleanup.complete"])
    publisher = HookEventPublisher(bus, source="daemon.pac")
    service = _service(tmp_path, clock)
    store: PacGraphStore | None = None
    try:
        graph_id = service.start(
            yaml_text=END_FLAG_WORKFLOW,
            sender=USER,
            operation_key="h5-cleanup",
        )["graphId"]
        store = PacGraphStore(service.database)
        runtime = _Runtime()
        coordinator = ActorCoordinator(
            store,
            runtime,
            daemon_epoch="epoch-1",
            resolver=FileLaunchResolver(tmp_path),
            sender=NullSender(),
            clock=lambda: clock[0],
            publisher=publisher,
        )
        coordinator.reconcile_all()
        assert len(runtime.running) == 1

        service._tick()
        work = _status_node(service, graph_id, "work")
        PacReactor(store, clock=service.clock).set_flag(
            graph_id,
            "work",
            actor=work["owner"],
            expected_request=work["requestId"],
            reason_ref="result:work",
        )
        service._tick()
        finish = _status_node(service, graph_id, "finish")
        PacReactor(store, clock=service.clock).set_flag(
            graph_id,
            "finish",
            actor=finish["owner"],
            expected_request=finish["requestId"],
            reason_ref="result:finish",
        )

        coordinator.reconcile_all()
        [event] = capture.wait_for(1)
        [receipt] = store.workflow_worker_receipts(graph_id)
        [intent] = store.workflow_worker_cleanup_intents(graph_id)
        assert dict(event.payload) == {
            "graphId": graph_id,
            "actorNode": receipt["actor_node"],
            "actorName": receipt["actor_name"],
            "operationId": intent["operation_id"],
            "workerState": "stopped",
            "atMs": 3_000,
            "collectionMethod": "daemon",
            "fidelity": "observed",
        }
        # A second reconcile must not re-emit the same cleanup.
        coordinator.reconcile_all()
        time.sleep(0.05)
        assert len(capture.snapshot()) == 1
    finally:
        if store is not None:
            store.close()
        assert bus.close(5.0)
        assert service.close()


def test_absent_publisher_leaves_close_facts_unchanged(tmp_path: Path) -> None:
    """Hooks off: no publisher means no event and the same durable close."""
    clock = [4_000]
    bus, capture = _bus_capture(["graph.closed"])
    service = _service(tmp_path, clock)
    store: PacGraphStore | None = None
    try:
        graph_id = service.start(
            yaml_text=END_FLAG_WORKFLOW,
            sender=USER,
            operation_key="h5-hooks-off",
        )["graphId"]
        store = PacGraphStore(service.database)
        _start_worker(store, tmp_path, clock)
        service._tick()
        work = _status_node(service, graph_id, "work")
        PacReactor(store, clock=service.clock).set_flag(
            graph_id,
            "work",
            actor=work["owner"],
            expected_request=work["requestId"],
            reason_ref="result:work",
        )
        service._tick()
        finish = _status_node(service, graph_id, "finish")
        PacReactor(store, clock=service.clock).set_flag(
            graph_id,
            "finish",
            actor=finish["owner"],
            expected_request=finish["requestId"],
            reason_ref="result:finish",
        )

        assert capture.snapshot() == []
        assert sum(row.accepted for row in bus.projection()) == 0
        graph = store.graph(graph_id)
        assert graph is not None and graph["closed_by"] == "finish"
        assert service.status(run_id=graph_id)["state"] == "completed"
    finally:
        if store is not None:
            store.close()
        assert bus.close(5.0)
        assert service.close()
