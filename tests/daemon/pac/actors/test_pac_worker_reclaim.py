"""Terminal PAC cleanup converges: every receipt-owned worker is reclaimed.

Production (daemon epoch 5eb369b2, 2026-10-18) held 45 terminal workers in
``workerCleanup: attention`` forever and 186 roster workers of closed graphs
without any cleanup intent, so the PAC GC never removed their agents.  These
tests drive the real graph authority, workflow service, coordinator and SQLite
store; only the harness process port is a fake, and its ``stop`` keeps the
production marker fence of ``DaemonActorRuntime.stop``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation
from hyprial.daemon.impl.pac.actors.daemon import (
    DaemonActorRuntime,
    PacActorService,
)
from hyprial.daemon.impl.pac.actors.gc import PacGc
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


USER = "user:test"
WORKFLOW = """
version: 2
name: reclaim
nodes:
  - id: work
    task: work
"""


class _Runtime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.stops: list[tuple[str, str, str]] = []
        self.stop_error: Exception | None = None
        self.start_error: Exception | None = None

    def observe(self, actor_name: str) -> RuntimeObservation:
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(self, actor_name, launch, *, operation_id, identity_marker):
        observation = RuntimeObservation(
            True, identity_marker, launch.harness, operation_id,
            f"entity-{actor_name}",
        )
        self.running[actor_name] = observation
        if self.start_error is not None:
            raise self.start_error
        return observation

    def stop(self, actor_name, launch, *, operation_id, identity_marker):
        self.stops.append((actor_name, operation_id, identity_marker))
        if self.stop_error is not None:
            raise self.stop_error
        current = self.running.get(actor_name)
        if current is None:
            return RuntimeObservation(False, operation_id=operation_id)
        if (current.identity_marker or "") != identity_marker:
            return current
        self.running.pop(actor_name, None)
        return RuntimeObservation(False, operation_id=operation_id)


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never converged"
        time.sleep(0.005)


def _managed(tmp_path: Path):
    state = tmp_path / "state"
    database = default_database_path(state)
    authority = PacGraphAuthority(database, NullSender())
    runtime = _Runtime()
    authority.attach_reconciler(runtime, tmp_path, "epoch")
    service = GraphWorkflowService(
        state_dir=state,
        owner="test",
        machine="machine",
        sender=NullSender(),
        graph_authority=authority,
        start_thread=True,
    )
    graph_id = service.start(
        yaml_text=WORKFLOW, sender=USER, operation_key=uuid4().hex,
    )["graphId"]
    store = PacGraphStore(database, read_only=True)
    try:
        actor = next(node for node in store.nodes(graph_id) if node.kind == "actor")
    finally:
        store.close()
    return authority, service, runtime, graph_id, actor


def _read(database: Path, fn):
    store = PacGraphStore(database, read_only=True)
    try:
        return fn(store)
    finally:
        store.close()


def _intent(database: Path, graph_id: str):
    rows = _read(database, lambda s: s.workflow_worker_cleanup_intents(graph_id))
    return rows[0] if rows else None


def _activation(database: Path, graph_id: str, node_id: str):
    return _read(database, lambda s: s._db.execute(
        "SELECT * FROM actor_activations WHERE graph_id=? AND node_id=?",
        (graph_id, node_id),
    ).fetchone())


def _state_is(database: Path, graph_id: str, state: str, reason=None):
    def check() -> bool:
        intent = _intent(database, graph_id)
        return (
            intent is not None
            and intent["state"] == state
            and (reason is None or intent["attention_reason"] == reason)
        )
    return check


def _started(authority, runtime, graph_id, actor) -> None:
    assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
    _wait_for(lambda: actor.actor_name in runtime.running)
    _wait_for(lambda: (_activation(authority.database, graph_id, actor.node_id) or {"op": None})["op"] == "done")


def test_transient_lifecycle_failure_is_retried_with_a_fresh_operation(
    tmp_path: Path,
) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        service.cancel(run_id=graph_id, actor=USER)
        runtime.stop_error = RuntimeError(
            "bind.release rejected: DOMAIN_COMMAND_TIMEOUT: domain command "
            "lifecycle:pac-cleanup:x:forward:2 did not settle"
        )
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(
            authority.database, graph_id, "attention",
            "lifecycle_manager_unavailable",
        ))

        runtime.stop_error = None
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))

        assert actor.actor_name not in runtime.running
        operations = [operation for _name, operation, _marker in runtime.stops]
        assert len(operations) == 2 and len(set(operations)) == 2
        receipts = _read(
            authority.database, lambda s: s.workflow_worker_receipts(graph_id)
        )
        assert receipts[0]["state"] == "down"
    finally:
        runtime.stop_error = None
        assert service.close()
        assert authority.close(3.0)


def test_a_failed_retry_is_recorded_and_the_next_retry_is_fresh(
    tmp_path: Path,
) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        service.cancel(run_id=graph_id, actor=USER)
        for attempt, detail in enumerate(("first unsettled", "second unsettled")):
            runtime.stop_error = RuntimeError(detail)
            assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"

            def recorded(expected=detail, count=attempt + 1) -> bool:
                intent = _intent(authority.database, graph_id)
                return (
                    intent is not None
                    and intent["attempts"] == count
                    and expected in (intent["last_observation_json"] or "")
                )

            _wait_for(recorded)
        runtime.stop_error = None
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))

        operations = [operation for _name, operation, _marker in runtime.stops]
        assert len(operations) == 3 and len(set(operations)) == 3
    finally:
        runtime.stop_error = None
        assert service.close()
        assert authority.close(3.0)


def test_launch_failed_worker_with_its_own_marker_is_reclaimed(
    tmp_path: Path,
) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        runtime.start_error = RuntimeError("ROUTE_IO_FAILED: compensated")
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(lambda: (
            _activation(authority.database, graph_id, actor.node_id) or {"op": None}
        )["op"] == "done")
        activation = _activation(authority.database, graph_id, actor.node_id)
        assert activation["identity_marker"] is None
        survivor = runtime.running[actor.actor_name]
        assert survivor.identity_marker.startswith(f"pac:{graph_id}:{actor.node_id}:1:")

        service.cancel(run_id=graph_id, actor=USER)
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))

        assert actor.actor_name not in runtime.running
        assert runtime.stops[-1][2] == survivor.identity_marker
        receipt = _read(
            authority.database, lambda s: s.workflow_worker_receipts(graph_id)
        )[0]
        assert receipt["state"] == "down"
        assert receipt["agent_entity_token"] == survivor.agent_entity_token
    finally:
        runtime.start_error = None
        assert service.close()
        assert authority.close(3.0)


def test_unmarked_restart_of_the_receipt_agent_is_reclaimed(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        token = runtime.running[actor.actor_name].agent_entity_token
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, None, "pi", None, agent_entity_token=token,
        )

        service.cancel(run_id=graph_id, actor=USER)
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))

        assert actor.actor_name not in runtime.running
        assert runtime.stops[-1][2] == ""
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_foreign_identity_still_fails_closed(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        runtime.running[actor.actor_name] = RuntimeObservation(
            True, None, "pi", None, agent_entity_token="replacement-entity",
        )
        service.cancel(run_id=graph_id, actor=USER)
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(
            authority.database, graph_id, "attention", "entity_token_mismatch",
        ))
        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        time.sleep(0.2)
        assert _intent(authority.database, graph_id)["state"] == "attention"
        assert runtime.running[actor.actor_name].agent_entity_token == "replacement-entity"
        assert runtime.stops == []
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_closed_graph_without_cleanup_intent_is_backfilled_and_reclaimed(
    tmp_path: Path,
) -> None:
    authority, service, runtime, graph_id, actor = _managed(tmp_path)
    try:
        _started(authority, runtime, graph_id, actor)
        service.cancel(run_id=graph_id, actor=USER)
        store = PacGraphStore(authority.database)
        try:
            with store.write():
                store._db.execute(
                    "DELETE FROM workflow_worker_cleanup_intents WHERE graph_id=?",
                    (graph_id,),
                )
        finally:
            store.close()

        assert authority.reconcile_actor(graph_id, actor.node_id).value == "accepted"
        _wait_for(_state_is(authority.database, graph_id, "complete"))
        assert actor.actor_name not in runtime.running
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_fallback_sweep_admits_attention_and_intentless_closed_workers(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    clock = [1000]
    service = GraphWorkflowService(
        state_dir=state, owner="test", machine="machine", sender=NullSender(),
        clock_ms=lambda: clock[0], start_thread=False,
    )
    try:
        attention = service.start(
            yaml_text=WORKFLOW, sender=USER, operation_key="attention",
        )["graphId"]
        intentless = service.start(
            yaml_text=WORKFLOW, sender=USER, operation_key="intentless",
        )["graphId"]
        service.cancel(run_id=attention, actor=USER)
        service.cancel(run_id=intentless, actor=USER)
        store = PacGraphStore(service.database)
        try:
            with store.write():
                store._db.execute(
                    "UPDATE workflow_worker_cleanup_intents SET state='attention',"
                    "attention_reason='lifecycle_manager_unavailable' "
                    "WHERE graph_id=?",
                    (attention,),
                )
                store._db.execute(
                    "DELETE FROM workflow_worker_cleanup_intents WHERE graph_id=?",
                    (intentless,),
                )
            nodes = {
                graph: next(
                    n.node_id for n in store.nodes(graph) if n.kind == "actor"
                )
                for graph in (attention, intentless)
            }
        finally:
            store.close()
    finally:
        assert service.close()

    actors = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=DaemonActorRuntime(SimpleNamespace()),
        sender=NullSender(),
        daemon_epoch="sweep",
        logger=lambda *args, **kwargs: None,
        workers=0,
    )
    try:
        actors._scan_and_admit(force_actors=True)
        admitted = set(actors._actor_queue.queue)
    finally:
        assert actors.close()
    assert (attention, nodes[attention]) in admitted
    assert (intentless, nodes[intentless]) in admitted


class _Spec:
    def __init__(self, nickname):
        self.nickname = nickname
        self.harness = "pi"


class _Application:
    def __init__(self, nickname) -> None:
        self.spec = _Spec(nickname)
        self.operations: list[object] = []
        self.desired_state = SimpleNamespace(
            harness_spec=lambda name, excluding=None: self.spec
        )
        self.agents = SimpleNamespace(get=lambda name: None)
        self.state_dir = None

    def _canonical_harness_uri(self, name, spec):
        return f"agent:test:machine:{name}"

    def _managed_worker_running(self, actor):
        return True

    def _lifecycle_spec(self, spec):
        return SimpleNamespace(interruption_reason=None)

    def _run_lifecycle_operation(self, operation):
        self.operations.append(operation)


def test_runtime_stop_accepts_the_unmarked_fence_only_for_unmarked_specs(
    monkeypatch,
) -> None:
    import hyprial.daemon.impl.pac.actors.daemon as actor_daemon

    monkeypatch.setattr(actor_daemon, "replace", lambda value, **_kw: value)
    monkeypatch.setattr(
        actor_daemon.LifecycleOperation, "deactivate",
        staticmethod(lambda operation_id, spec: ("deactivate", operation_id)),
    )
    launch = SimpleNamespace()
    unmarked = _Application(None)
    DaemonActorRuntime(unmarked).stop(
        "wf-a", launch, operation_id="op-unmarked", identity_marker="",
    )
    assert unmarked.operations == [("deactivate", "op-unmarked")]

    marked = _Application("pac:wf-other:_actor.x:1:effect")
    DaemonActorRuntime(marked).stop(
        "wf-a", launch, operation_id="op-marked", identity_marker="",
    )
    assert marked.operations == []


def test_gc_preview_is_served_by_the_daemon_router(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    PacGraphStore(default_database_path(state)).close()
    gc = PacGc(
        state_dir=state,
        application=SimpleNamespace(agents=SimpleNamespace(get=lambda _n: None)),
        runtime=_Runtime(),
        logger=lambda *args, **kwargs: None,
        start_thread=False,
    )
    app = object.__new__(DaemonApplication)
    app.owner = "test"
    app._restore_done = threading.Event()
    app._restore_done.set()
    app._inbox = object()
    app._harnesses = object()
    app._presence = object()
    app._pac_gc = gc
    try:
        preview = app.handle("pac.gc.preview", {})
    finally:
        gc.close()
    assert preview["ok"] is True and preview["dryRun"] is True
    assert preview["removableBacklog"] == 0
