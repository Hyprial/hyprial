"""PAC graph writer commits flags before outbound notification effects."""

from __future__ import annotations

import time
import threading
import json
from pathlib import Path
from uuid import uuid4

import pytest

import hyprial.daemon.impl.pac.graphs.authority as authority_module
import hyprial.daemon.impl.pac.graphs.authority.generation as authority_generation
from hyprial.daemon.impl.pac.graphs.authority import (
    PacGraphAuthority,
    PacGraphTimeout,
    _ActorEffect,
    _ActorEffectCompleted,
)
from hyprial.daemon.impl.pac.graphs.edits import add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.contracts.restore import PacRestoreFacts
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService


OWNER = "user:hyprial-unit-owner"
BORROWED = "agent:test:machine:wf-borrowed-adversary"


class _ActorRuntime:
    def __init__(self) -> None:
        self.running: dict[str, RuntimeObservation] = {}
        self.stop_error: Exception | None = None
        self.observe_error: Exception | None = None
        self.stops: list[tuple[str, str]] = []

    def observe(self, actor_name: str) -> RuntimeObservation:
        if self.observe_error is not None:
            raise self.observe_error
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(self, actor_name, launch, *, operation_id, identity_marker):
        result = RuntimeObservation(
            True,
            identity_marker,
            launch.harness,
            operation_id,
            f"entity-{actor_name}",
        )
        self.running[actor_name] = result
        return result

    def stop(self, actor_name, launch, *, operation_id, identity_marker):
        self.stops.append((actor_name, operation_id))
        if self.stop_error is not None:
            raise self.stop_error
        self.running.pop(actor_name, None)
        return RuntimeObservation(False, operation_id=operation_id)


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def _activation_done(database: Path, graph_id: str, node_id: str) -> bool:
    store = PacGraphStore(database, read_only=True)
    try:
        row = store._db.execute(
            "SELECT op FROM actor_activations WHERE graph_id=? AND node_id=?",
            (graph_id, node_id),
        ).fetchone()
        return row is not None and row["op"] == "done"
    finally:
        store.close()


def _node_flagged(database: Path, graph_id: str, node_id: str) -> bool:
    store = PacGraphStore(database, read_only=True)
    try:
        node = store.node(graph_id, node_id)
        return node is not None and node.flag
    finally:
        store.close()


def _managed_authority(tmp_path: Path, yaml_text: str | None = None):
    state = tmp_path / "state"
    database = default_database_path(state)
    authority = PacGraphAuthority(database, NullSender())
    runtime = _ActorRuntime()
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
        yaml_text=yaml_text or """
version: 2
name: cleanup-authority
nodes:
  - id: work
    task: work
""",
        sender="user:test",
        operation_key=uuid4().hex,
    )["graphId"]
    store = PacGraphStore(database, read_only=True)
    try:
        actor_node = next(
            (node for node in store.nodes(graph_id) if node.kind == "actor"),
            None,
        )
    finally:
        store.close()
    return authority, service, runtime, graph_id, actor_node


def _actor_flag_committed(authority, graph_id, node_id) -> bool:
    store = PacGraphStore(authority.database, read_only=True)
    try:
        node = store.node(graph_id, node_id)
        return node is not None and bool(node.flag)
    finally:
        store.close()


def _graph(tmp_path: Path) -> tuple[Path, str]:
    database = default_database_path(tmp_path)
    store = PacGraphStore(database)
    graph_id = create_graph(store, name="authority", created_by=OWNER)["graphId"]
    version = add_node(
        store, tmp_path, graph_id=graph_id, node_id="first", owner=OWNER,
        brief_ref="brief:first", expect_version=1,
    )["version"]
    version = add_node(
        store, tmp_path, graph_id=graph_id, node_id="second", owner=OWNER,
        brief_ref="brief:second", expect_version=version,
    )["version"]
    add_edge(
        store, graph_id=graph_id, from_node="first", to_node="second",
        expect_version=version,
    )
    store.close()
    return database, graph_id


def test_graph_authority_commits_flag_and_delivers_from_durable_outbox(
    tmp_path: Path,
) -> None:
    database, graph_id = _graph(tmp_path)

    sender = NullSender()
    authority = PacGraphAuthority(database, sender)
    try:
        assert authority.activate_graph(graph_id, actor=OWNER)["ok"]
        result = authority.set_flag(graph_id, "first", actor=OWNER)
        assert result["deliveryPending"] and result["undelivered"] == 1
        deadline = time.monotonic() + 3.0
        while len(sender.sent) < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        deadline = time.monotonic() + 3.0
        while True:
            read = PacGraphStore(database, read_only=True)
            try:
                rows = read.notifications(graph_id)
                delivered = len(rows) == 1 and rows[0].message_id is not None
            finally:
                read.close()
            if delivered:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert sender.sent[0]["idempotencyKey"].startswith("pac-notify:")
    finally:
        assert authority.close()


def test_slow_delivery_does_not_block_writer_and_restart_replays_outbox(
    tmp_path: Path,
) -> None:
    database, graph_id = _graph(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class SlowSender(NullSender):
        def send(self, **kwargs):
            entered.set()
            assert release.wait(3.0)
            return super().send(**kwargs)

    first = PacGraphAuthority(database, SlowSender())
    try:
        first.activate_graph(graph_id, actor=OWNER)
        started = time.monotonic()
        assert first.set_flag(graph_id, "first", actor=OWNER)["deliveryPending"]
        assert time.monotonic() - started < 0.25
        assert entered.wait(1.0)
        assert first.close(0.02) is False
    finally:
        release.set()
        first.close(3.0)

    replacement_sender = NullSender()
    replacement = PacGraphAuthority(database, replacement_sender)
    try:
        deadline = time.monotonic() + 3.0
        while not replacement_sender.sent:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert len(replacement_sender.sent) == 1
    finally:
        assert replacement.close()


def test_legacy_delivery_lane_does_not_claim_workflow_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, graph_id = _graph(tmp_path)
    store = PacGraphStore(database)
    try:
        with store.write():
            store._db.execute(
                "INSERT INTO workflow_graphs(graph_id,specification_ref,specification_digest,on_failure,state) "
                "VALUES (?,?,?,'terminate','running')",
                (graph_id, "spec:test", "digest:test"),
            )
            store._db.execute(
                "INSERT INTO notifications(event_id,edge,kind,recipient,text,sender,at,plan_json) "
                "VALUES (?,?, 'turn', ?,?,?,1,?)",
                (
                    "workflow-request:test", "request", OWNER, "request text", OWNER,
                    json.dumps({"graphId": graph_id}),
                ),
            )
    finally:
        store.close()
    sender = NullSender()
    authority = PacGraphAuthority(database, sender)
    try:
        assert authority._scan_deliveries() == ()
        scanned = threading.Event()
        original_scan = authority._scan_deliveries

        def observed_scan():
            # The delivery loop exhausts this iterator only after processing
            # every candidate, so the event observes a whole scan iteration.
            yield from original_scan()
            scanned.set()

        monkeypatch.setattr(authority, "_scan_deliveries", observed_scan)
        authority._wake.set()
        _wait_for(scanned.is_set)
        assert authority.close()
        assert sender.sent == []
    finally:
        assert authority.close()


def test_stale_actor_completion_cannot_release_new_generation_custody(
    tmp_path: Path,
) -> None:
    database, graph_id = _graph(tmp_path)
    authority = PacGraphAuthority(database, NullSender())
    try:
        with authority._actor_lock:
            authority._actor_active[(graph_id, "worker")] = 2
        authority._release_actor_effect(graph_id, "worker", 1)
        assert authority.actor_effect_stats()["inFlight"] == 1
        authority._release_actor_effect(graph_id, "worker", 2)
        assert authority.actor_effect_stats()["inFlight"] == 0
    finally:
        assert authority.close()


def test_restore_facts_read_is_nonblocking_immutable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _graph_id = _graph(tmp_path)
    authority = PacGraphAuthority(database, NullSender())
    try:
        expected = authority.restore_facts("unknown")
        assert expected == PacRestoreFacts()
        monkeypatch.setattr(
            authority_module,
            "PacGraphStore",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("restore_facts performed caller-side I/O")
            ),
        )
        authority._refresh_restore_facts()
        assert authority.restore_facts("unknown") is expected
    finally:
        assert authority.close()


def test_timed_out_graph_command_retires_capacity_and_keeps_bounded_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, graph_id = _graph(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = authority_generation.activate_graph

    def slow_activate(*args, **kwargs):
        entered.set()
        assert release.wait(3.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(authority_generation, "activate_graph", slow_activate)
    authority = PacGraphAuthority(
        database,
        NullSender(),
        mailbox_capacity=1,
        completion_capacity=1,
        call_timeout=0.02,
    )
    try:
        with pytest.raises(PacGraphTimeout) as caught:
            authority.activate_graph(graph_id, actor=OWNER)
        assert entered.is_set()
        release.set()
        completion = authority.settled_result(
            caught.value.correlation_id, timeout=3.0
        )
        assert completion is not None and completion.error is None
        assert completion.result is not None and completion.result["ok"] is True
        assert authority._pending == {}
        # The abandoned completion no longer consumes the sole active slot.
        assert authority.close_graph(graph_id, actor=OWNER)["ok"] is True
    finally:
        release.set()
        assert authority.close(3.0)


def test_terminal_cleanup_runs_through_graph_authority_only(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor_node = _managed_authority(tmp_path)
    try:
        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"
        _wait_for(lambda: _actor_flag_committed(authority, graph_id, actor_node.node_id))

        authority.workflow_tick(service.clock())
        requested = authority.restore_facts(actor_node.actor_name)
        assert requested.requested and requested.pending_work
        assert not requested.terminal

        status = service.cancel(run_id=graph_id, actor="user:test")
        assert status["workerCleanup"] == "pending"
        assert authority.restore_facts(actor_node.actor_name).terminal
        assert not authority.restore_facts(actor_node.actor_name).pending_work

        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"
        _wait_for(
            lambda: service.status(run_id=graph_id).get("workerCleanup")
            == "complete"
        )
        assert runtime.running == {}
        assert len(runtime.stops) == 1
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_managed_end_flag_creates_cleanup_before_reclaiming_owned_worker(
    tmp_path: Path,
) -> None:
    authority, service, runtime, graph_id, actor_node = _managed_authority(
        tmp_path,
        """
version: 2
name: end-cleanup-authority
nodes:
  - id: work
    task: work
  - id: finish
    kind: end
    owner: user:test
    task: finish
    after: [work]
""",
    )
    service_closed = False
    try:
        assert actor_node is not None
        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"
        _wait_for(
            lambda: _node_flagged(
                authority.database, graph_id, actor_node.node_id
            )
        )
        authority.workflow_tick(service.clock())
        work = next(
            node
            for node in service.status(run_id=graph_id)["nodes"]
            if node["nodeId"] == "work"
        )
        authority.set_flag(
            graph_id,
            "work",
            actor=work["owner"],
            expected_request=work["requestId"],
            reason_ref="result:work",
        )
        authority.workflow_tick(service.clock())
        finish = next(
            node
            for node in service.status(run_id=graph_id)["nodes"]
            if node["nodeId"] == "finish"
        )
        assert service.close()
        service_closed = True

        result = authority.set_flag(
            graph_id,
            "finish",
            actor="user:test",
            expected_request=finish["requestId"],
            reason_ref="result:accepted",
            output_text="accepted output",
        )
        facts = authority.restore_facts(actor_node.actor_name)
        assert facts.terminal and not facts.pending_work

        store = PacGraphStore(authority.database, read_only=True)
        try:
            graph = store.graph(graph_id)
            assert graph is not None and graph["closed_by"] == "finish"
            workflow = store._db.execute(
                "SELECT state,reason_ref FROM workflow_graphs WHERE graph_id=?",
                (graph_id,),
            ).fetchone()
            assert tuple(workflow) == ("completed", "result:accepted")
            finish_row = store._db.execute(
                "SELECT state,reason_ref,output_text FROM workflow_nodes "
                "WHERE graph_id=? AND node_id='finish'",
                (graph_id,),
            ).fetchone()
            assert tuple(finish_row) == (
                "done",
                "result:accepted",
                "accepted output",
            )
            [intent] = store.workflow_worker_cleanup_intents(graph_id)
            assert intent["actor_name"] == actor_node.actor_name
            assert intent["state"] == "pending"
        finally:
            store.close()
        assert any(
            item["edge"].startswith("workflow-output:finish:")
            and "accepted output" in item["text"]
            for item in result["notifications"]
        )

        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"

        def cleanup_committed() -> bool:
            read = PacGraphStore(authority.database, read_only=True)
            try:
                cleanup = read.workflow_worker_cleanup_intents(graph_id)
                receipts = read.workflow_worker_receipts(graph_id)
                return (
                    len(cleanup) == len(receipts) == 1
                    and cleanup[0]["operation_id"] == intent["operation_id"]
                    and cleanup[0]["state"] == "complete"
                    and receipts[0]["state"] == "down"
                )
            finally:
                read.close()

        # Native stop precedes its writer-owned durable completion.
        _wait_for(cleanup_committed)
        assert runtime.running == {}
        store = PacGraphStore(authority.database, read_only=True)
        try:
            assert store.workflow_worker_cleanup_intents(graph_id)[0]["state"] == "complete"
            assert store.workflow_worker_receipts(graph_id)[0]["state"] == "down"
        finally:
            store.close()
        assert len(runtime.stops) == 1
    finally:
        if not service_closed:
            assert service.close()
        assert authority.close(3.0)


def test_managed_end_flag_never_claims_or_stops_borrowed_actor(tmp_path: Path) -> None:
    authority, service, runtime, graph_id, actor_node = _managed_authority(
        tmp_path,
        f"""
version: 2
name: end-borrowed-authority
nodes:
  - id: borrowed
    owner: {BORROWED}
    task: borrowed
  - id: finish
    kind: end
    owner: user:test
    task: finish
    after: [borrowed]
""",
    )
    service_closed = False
    runtime.running[BORROWED] = RuntimeObservation(
        True, "borrowed-marker", "codex"
    )
    try:
        assert actor_node is None
        authority.workflow_tick(service.clock())
        borrowed = next(
            node
            for node in service.status(run_id=graph_id)["nodes"]
            if node["nodeId"] == "borrowed"
        )
        authority.set_flag(
            graph_id,
            "borrowed",
            actor=BORROWED,
            expected_request=borrowed["requestId"],
            reason_ref="result:borrowed",
        )
        authority.workflow_tick(service.clock())
        finish = next(
            node
            for node in service.status(run_id=graph_id)["nodes"]
            if node["nodeId"] == "finish"
        )
        assert service.close()
        service_closed = True

        authority.set_flag(
            graph_id,
            "finish",
            actor="user:test",
            expected_request=finish["requestId"],
            reason_ref="result:accepted",
        )

        store = PacGraphStore(authority.database, read_only=True)
        try:
            assert store.workflow_worker_receipts(graph_id) == []
            assert store.workflow_worker_cleanup_intents(graph_id) == []
            assert store.workflow_worker_cleanup(graph_id) == "complete"
        finally:
            store.close()
        assert runtime.running[BORROWED].present
        assert runtime.stops == []
    finally:
        if not service_closed:
            assert service.close()
        assert authority.close(3.0)


def test_restore_facts_refreshes_at_set_flag_commit(tmp_path: Path) -> None:
    authority, service, _runtime, graph_id, actor_node = _managed_authority(tmp_path)
    try:
        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"
        _wait_for(lambda: _actor_flag_committed(authority, graph_id, actor_node.node_id))
        authority.workflow_tick(service.clock())
        before = authority.restore_facts(actor_node.actor_name)
        assert before.requested and before.pending_work
        # Stop the delivery/timer worker after establishing requested state;
        # only the command under test may refresh the snapshot now.
        assert service.close()

        [work] = service.status(run_id=graph_id)["nodes"]
        authority.set_flag(
            graph_id,
            work["nodeId"],
            actor=work["owner"],
            expected_request=work["requestId"],
            reason_ref="result:test",
        )

        after = authority.restore_facts(actor_node.actor_name)
        assert not after.requested
        assert not after.pending_work
        assert not after.terminal
    finally:
        assert service.close()
        assert authority.close(3.0)


def test_restore_facts_refreshes_at_close_graph_commit(tmp_path: Path) -> None:
    authority, service, _runtime, graph_id, actor_node = _managed_authority(tmp_path)
    try:
        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"
        _wait_for(lambda: _actor_flag_committed(authority, graph_id, actor_node.node_id))
        authority.workflow_tick(service.clock())
        before = authority.restore_facts(actor_node.actor_name)
        assert before.requested and not before.terminal
        assert service.close()

        authority.close_graph(graph_id, actor="user:test")

        after = authority.restore_facts(actor_node.actor_name)
        assert after.terminal
        assert not after.requested
        assert not after.pending_work
        store = PacGraphStore(authority.database, read_only=True)
        try:
            [intent] = store.workflow_worker_cleanup_intents(graph_id)
            assert intent["actor_name"] == actor_node.actor_name
            assert intent["state"] == "pending"
        finally:
            store.close()
    finally:
        assert service.close()
        assert authority.close(3.0)


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    [("observe", "lifecycle_manager_unavailable"), ("stop", "stop_timeout")],
)
def test_cleanup_effect_error_commits_attention_on_writer(
    tmp_path: Path, kind: str, expected_reason: str
) -> None:
    authority, service, runtime, graph_id, actor_node = _managed_authority(tmp_path)
    try:
        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"
        _wait_for(lambda: actor_node.actor_name in runtime.running)
        _wait_for(
            lambda: _activation_done(
                authority.database, graph_id, actor_node.node_id
            )
        )
        service.cancel(run_id=graph_id, actor="user:test")
        if kind == "observe":
            runtime.observe_error = RuntimeError("observe unavailable")
        else:
            runtime.stop_error = TimeoutError("stop unsettled")

        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"

        def attention() -> bool:
            store = PacGraphStore(authority.database, read_only=True)
            try:
                [intent] = store.workflow_worker_cleanup_intents(graph_id)
                return (
                    intent["state"] == "attention"
                    and intent["attention_reason"] == expected_reason
                )
            finally:
                store.close()

        _wait_for(attention)
        store = PacGraphStore(authority.database, read_only=True)
        try:
            [intent] = store.workflow_worker_cleanup_intents(graph_id)
            observation = json.loads(intent["last_observation_json"])
            assert observation["errorType"] in {"RuntimeError", "TimeoutError"}
            assert observation.get("present") is (True if kind == "stop" else None)
        finally:
            store.close()
        assert runtime.running
    finally:
        runtime.observe_error = None
        runtime.stop_error = None
        assert service.close()
        assert authority.close(3.0)


def test_non_expansion_observe_failure_skips_worktree_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, service, runtime, graph_id, actor_node = _managed_authority(tmp_path)
    statements: list[str] = []
    failures: list[str] = []
    original_store = authority_generation.PacGraphStore

    def traced_store(*args, **kwargs):
        store = original_store(*args, **kwargs)
        store._db.set_trace_callback(statements.append)
        return store

    monkeypatch.setattr(authority_generation, "PacGraphStore", traced_store)
    authority._actor_logger = (
        lambda _level, _component, _event, **fields: failures.append(
            fields["detail"]
        )
    )
    try:
        runtime.observe_error = RuntimeError("observe unavailable")
        assert authority.reconcile_actor(graph_id, actor_node.node_id).value == "accepted"
        _wait_for(lambda: any("observe unavailable" in item for item in failures))
        assert not any(
            "UPDATE workflow_worktrees" in statement for statement in statements
        )
    finally:
        runtime.observe_error = None
        assert service.close()
        assert authority.close(3.0)


def test_stale_cleanup_stop_completion_cannot_complete_replaced_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, service, runtime, graph_id, actor_node = _managed_authority(tmp_path)
    service_closed = False
    try:
        service.cancel(run_id=graph_id, actor="user:test")
        assert service.close()
        service_closed = True
        store = PacGraphStore(authority.database)
        try:
            [intent] = store.workflow_worker_cleanup_intents(graph_id)
            assert intent["state"] == "pending"
            with store.write():
                store._db.execute(
                    "INSERT INTO actor_activations "
                    "(graph_id,node_id,incarnation,desired,op,effect_id,"
                    "operation_id,updated_at) VALUES (?,?,0,'down','pending',"
                    "'replacement-effect','replacement',1)",
                    (graph_id, actor_node.node_id),
                )
            assert store._db.execute(
                "SELECT operation_id FROM actor_activations "
                "WHERE graph_id=? AND node_id=?",
                (graph_id, actor_node.node_id),
            ).fetchone()["operation_id"] == "replacement"
        finally:
            store.close()
        before_completion = PacGraphStore(authority.database, read_only=True)
        try:
            assert before_completion.workflow_worker_cleanup_intents(graph_id)[0][
                "state"
            ] == "pending"
        finally:
            before_completion.close()
        generation = authority._runtime.snapshot(authority._handle).generation
        effect = _ActorEffect(
            generation,
            graph_id,
            actor_node.node_id,
            "stop",
            actor_node.actor_name,
            operation_id=intent["operation_id"],
            expected_activation_id=intent["operation_id"],
        )
        completion = _ActorEffectCompleted(
            generation, effect, observation=RuntimeObservation(False)
        )
        processed = threading.Event()
        original_completed = authority_module._Generation._actor_effect_completed

        def observed_completed(writer, current):
            original_completed(writer, current)
            if current is completion:
                processed.set()

        monkeypatch.setattr(
            authority_module._Generation, "_actor_effect_completed", observed_completed
        )
        assert authority._runtime.tell(authority._handle, completion).value == "accepted"
        _wait_for(processed.is_set)
        read = PacGraphStore(authority.database, read_only=True)
        try:
            [after] = read.workflow_worker_cleanup_intents(graph_id)
            assert after["state"] == "pending"
            assert read.workflow_worker_receipts(graph_id)[0]["state"] == "planned"
        finally:
            read.close()
    finally:
        if not service_closed:
            assert service.close()
        assert authority.close(3.0)


def test_legacy_delivery_lane_terminalizes_a_permanent_failure_once(
    tmp_path: Path,
) -> None:
    """#987 on the actorized legacy lane: a definitively permanent send error
    is recorded terminally through the writer and the row is never re-sent;
    before the port, _delivery_loop swallowed every error and retried forever.
    """

    from hyprial.kernel import ipc_errors
    from hyprial.daemon.impl.inbox.links.io import InboxIoError

    database, graph_id = _graph(tmp_path)
    attempts: list[dict] = []
    logs: list[tuple[str, str, str, dict]] = []

    class RefusingSender(NullSender):
        def send(self, **kwargs):
            attempts.append(kwargs)
            raise InboxIoError(
                "the live proxy rejected the notification sender",
                permanent=True,
                code=ipc_errors.SENDER_UNRESOLVED,
            )

    def logger(level, component, event, **fields):
        logs.append((level, component, event, fields))

    authority = PacGraphAuthority(database, RefusingSender(), logger=logger)
    try:
        authority.activate_graph(graph_id, actor=OWNER)
        assert authority.set_flag(graph_id, "first", actor=OWNER)["deliveryPending"]
        deadline = time.monotonic() + 3.0
        while True:
            read = PacGraphStore(database, read_only=True)
            try:
                rows = read.notifications(graph_id)
                failed = len(rows) == 1 and rows[0].failed_at is not None
            finally:
                read.close()
            if failed:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # Several more delivery-loop wakes must not re-send the failed row.
        for _ in range(3):
            authority._wake.set()
            time.sleep(0.05)
        assert len(attempts) == 1
        written = [entry for entry in logs if entry[2] == "workflow.delivery_failed"]
        assert len(written) == 1
        assert written[0][3]["code"] == ipc_errors.SENDER_UNRESOLVED
    finally:
        assert authority.close()
