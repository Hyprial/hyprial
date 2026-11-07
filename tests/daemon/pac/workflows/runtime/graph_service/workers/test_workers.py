import pytest
import time
import threading
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.storage.store import default_database_path
from hyprial.daemon.impl.pac.actors.coordinator import ActorCoordinator, FileLaunchResolver
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

from ..support import (
    AGENT,
    Runtime,
    Sender,
    USER,
    bound_message,
    complete,
    service as service,
    start,
)


def _seed_pending(state_dir, item: HarnessDelivery) -> None:
    now_ms = time.time_ns() // 1_000_000
    inbox = InboxService(state_dir / "inbox.sqlite3", MemoryDeliveryTransport())
    try:
        assert inbox.receive(
            InboxMessage(
                message_id=item.delivery_id,
                conversation_id=item.conversation_id,
                sender="agent:test:sender:requester",
                recipient=item.recipient,
                payload=item.message.encode(),
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=now_ms,
            ),
            now_ms=now_ms,
        ).acknowledged
    finally:
        inbox.close()


def test_workflow_delivery_receipt_returns_to_graph_writer(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sender = Sender()
    events = []
    authority = PacGraphAuthority(default_database_path(state_dir), sender)
    service = GraphWorkflowService(
        state_dir=state_dir, owner="test", machine="machine", sender=sender,
        start_thread=True, delivery_receipt=authority,
        graph_authority=authority,
        logger=lambda *args, **fields: events.append((args, fields)),
    )
    ticks: list[str] = []
    original_tick = service._tick

    def observed_tick(at):
        ticks.append(threading.current_thread().name)
        return original_tick(at)

    service._tick = observed_tick
    try:
        graph = start(service, [{"id": "work", "owner": AGENT, "task": "do it"}])
        service.admit = lambda *_: pytest.fail("workflow replay was re-admitted")
        assert start(service, [{"id": "work", "owner": AGENT, "task": "do it"}]) == graph
        deadline = time.monotonic() + 3.0
        while not sender.messages:
            assert time.monotonic() < deadline, events
            time.sleep(0.01)
        assert ticks and all(name != "hyprial-pac-workflow" for name in ticks)
        while True:
            store = PacGraphStore(service.database, read_only=True)
            try:
                rows = [
                    *store.notifications(graph),
                    *store.notifications_by_synthetic_event(graph),
                ]
                delivered = bool(rows and rows[0].message_id)
            finally:
                store.close()
            if delivered:
                break
            assert time.monotonic() < deadline, events
            time.sleep(0.01)
    finally:
        assert service.close()
        assert authority.close()


def test_owned_workers_launch_lazily_and_close_reclaims_all(service, tmp_path):
    svc, sender, _ = service
    graph = start(
        svc,
        [
            {"id": "first", "task": "first"},
            {"id": "second", "task": "second", "after": ["first"]},
        ],
    )
    runtime = Runtime()
    store = PacGraphStore(svc.database)
    actor = ActorCoordinator(
        store,
        runtime,
        daemon_epoch="test",
        resolver=FileLaunchResolver(tmp_path),
        clock=svc.clock,
    )
    try:
        actor.reconcile_all()
        assert len(runtime.starts) == 1
        svc._tick()
        assert len(sender.messages) == 1
        complete(svc, graph, "first")
        actor.reconcile_all()
        assert len(runtime.starts) == 2
        svc._tick()
        complete(svc, graph, "second")
        actor.reconcile_all()
        assert len(runtime.stops) == 2
        assert runtime.running == {}
        assert svc.status(run_id=graph)["state"] == "completed"
        assert all(n["state"] == "done" for n in svc.status(run_id=graph)["nodes"])
    finally:
        store.close()


def test_managed_worker_receipt_survives_actorized_reconcile(service, tmp_path):
    svc, sender, _clock = service
    graph = start(svc, [{"id": "first", "task": "first"}])
    store = PacGraphStore(svc.database)
    actor_nodes = [node for node in store.nodes(graph) if node.kind == "actor"]
    assert len(actor_nodes) == 1
    runtime = Runtime()
    authority = PacGraphAuthority(svc.database, sender)
    authority.attach_reconciler(runtime, tmp_path, "test")
    try:
        node = actor_nodes[0]
        assert authority.reconcile_actor(graph, node.node_id).value == "accepted"
        deadline = time.monotonic() + 3.0
        while not runtime.starts:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        receipt = store.workflow_worker_receipt(graph, node.node_id)
        assert receipt is not None
        assert receipt["launch_digest"]
        assert store.workflow_roster(graph) is not None
    finally:
        assert authority.close(3.0)
        store.close()


def test_explicit_shared_worker_starts_once(service, tmp_path):
    svc, _, _ = service
    graph = start(
        svc,
        [
            {"id": "a", "task": "a", "worker": "shared"},
            {"id": "b", "task": "b", "worker": "shared", "after": ["a"]},
        ],
        workers={"shared": {"tier": "fast"}},
    )
    runtime = Runtime()
    store = PacGraphStore(svc.database)
    actor = ActorCoordinator(
        store,
        runtime,
        daemon_epoch="test",
        resolver=FileLaunchResolver(tmp_path),
        clock=svc.clock,
    )
    try:
        actor.reconcile_all()
        svc._tick()
        complete(svc, graph, "a")
        actor.reconcile_all()
        svc._tick()
        complete(svc, graph, "b")
        actor.reconcile_all()
        assert len(runtime.starts) == len(runtime.stops) == 1
    finally:
        store.close()


def test_interrupted_outcome_after_graph_close_is_a_noop(service):
    svc, _, _ = service
    graph = start(svc, [{"id": "work", "task": "work", "owner": AGENT}])
    svc._tick()
    message = bound_message(svc)
    svc.cancel(run_id=graph, actor=USER)

    assert svc.record_harness_outcome(
        message_id=message,
        recipient=AGENT,
        failed=True,
        failure_code=None,
    )
    status = svc.status(run_id=graph)
    assert status["state"] == "cancelled"
    assert status["nodes"][0]["state"] == "cancelled"


def test_queued_withdrawn_request_never_reaches_model_but_ordinary_work_does(service):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace
    from hyprial.daemon.impl.harnesses.streaming.process import SequentialTurnProcess
    from hyprial.daemon.impl.pac.contracts.delivery import WITHDRAWN

    svc, _, clock = service
    clock[0] = time.time_ns() // 1_000_000
    graph = start(svc, [{"id": "work", "task": "work", "owner": AGENT}])
    svc._tick()
    message = bound_message(svc)
    entered, release = threading.Event(), threading.Event()
    queries = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            queries.append(prompt)
            entered.set()

        async def receive_response(self):
            while not release.is_set():
                await asyncio.sleep(0.005)
            yield SimpleNamespace(result="ordinary result", is_error=False)

        async def interrupt(self):
            release.set()

    process = SequentialTurnProcess(
        harness="codex",
        label="guard-fixture",
        client_factory=Client,
        thread_name="pac-guard-fixture",
    )
    process.worker_channel = SimpleNamespace(state_dir=svc.database.parent)
    ordinary = HarnessDelivery("ordinary", "chat", USER, AGENT, "ordinary work")
    workflow = HarnessDelivery(
        message, "pac-" + graph, USER, AGENT, "withdrawn work"
    )
    _seed_pending(svc.database.parent, ordinary)
    _seed_pending(svc.database.parent, workflow)
    try:
        assert process.enqueue(ordinary)
        assert entered.wait(2)
        assert process.enqueue(workflow)
        svc.cancel(run_id=graph, actor=USER)
        release.set()
        results = []
        deadline = time.monotonic() + 3
        while len(results) < 2 and time.monotonic() < deadline:
            results.extend(process.drain_results())
            time.sleep(0.005)
        assert len(results) == 2
        assert queries == [
            "[Harness Network message from user:test to agent:test:machine:existing]\nordinary work"
        ]
        assert (
            next(r for r in results if r.delivery_id == message).failure_code
            == WITHDRAWN
        )
    finally:
        release.set()
        process.stop()


def test_replay_does_not_readmit_an_already_finished_borrowed_actor(service):
    svc, _, _ = service
    nodes = [{"id": "work", "task": "work", "owner": AGENT}]
    graph = start(svc, nodes)
    svc._tick()
    complete(svc, graph, "work")
    svc.admit = lambda *_: pytest.fail("replay tried to re-admit a completed actor")
    assert start(svc, nodes) == graph


def test_harness_disconnect_is_one_attempt_for_graph_work_but_keeps_chat_retry_policy(
    service,
):
    import time
    from collections import Counter
    from types import SimpleNamespace
    from hyprial.daemon.impl.harnesses.streaming.process import SequentialTurnProcess

    svc, _, clock = service
    clock[0] = time.time_ns() // 1_000_000
    start(svc, [{"id": "work", "task": "work", "owner": AGENT}])
    svc._tick()
    message = bound_message(svc)
    queries = Counter()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            queries[prompt] += 1
            raise ConnectionError("fixture transport broke")

        async def receive_response(self):
            yield SimpleNamespace(result="", is_error=False)

        async def interrupt(self):
            pass

    process = SequentialTurnProcess(
        harness="codex",
        label="no-retry-fixture",
        client_factory=Client,
        thread_name="pac-no-retry-fixture",
        reconnect_delay_seconds=0.001,
        reconnect_delay_max_seconds=0.001,
        max_delivery_attempts=3,
    )
    process.worker_channel = SimpleNamespace(state_dir=svc.database.parent)
    workflow = HarnessDelivery(message, "graph", USER, AGENT, "graph work")
    ordinary = HarnessDelivery("ordinary", "chat", USER, AGENT, "ordinary work")
    _seed_pending(svc.database.parent, workflow)
    _seed_pending(svc.database.parent, ordinary)
    try:
        assert process.enqueue(workflow)
        assert process.enqueue(ordinary)
        results = []
        deadline = time.monotonic() + 3
        while len(results) < 2 and time.monotonic() < deadline:
            results.extend(process.drain_results())
            time.sleep(0.005)
        assert len(results) == 2
        assert queries == {
            "[Harness Network message from user:test to agent:test:machine:existing]\ngraph work": 1,
            "[Harness Network message from user:test to agent:test:machine:existing]\nordinary work": 3,
        }
    finally:
        process.stop()
