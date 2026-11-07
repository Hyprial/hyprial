from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

from ..support import (
    AGENT,
    complete,
    service as service,
    start,
)


def test_deadline_is_fixed_and_not_extended_by_ticks(service):
    svc, sender, clock = service
    graph = start(svc, [{"id": "a", "task": "a", "owner": AGENT, "timeout": "1s"}])
    svc._tick()
    clock[0] = 2000
    svc._tick()
    assert svc.status(run_id=graph)["state"] == "running"
    clock[0] = 2001
    svc._tick()
    assert svc.status(run_id=graph)["state"] == "failed"
    old_count = len(sender.messages)
    clock[0] += 10000
    svc._tick()
    assert len(sender.messages) == old_count


def test_timeout_starts_when_node_is_first_requested(service):
    svc, _, clock = service
    graph = start(
        svc,
        [
            {"id": "upstream", "task": "upstream", "owner": AGENT},
            {
                "id": "work",
                "task": "work",
                "owner": AGENT,
                "after": ["upstream"],
                "timeout": "1s",
            },
        ],
        escalate_to="user:escalation",
    )
    svc._tick()

    pending = next(
        node for node in svc.status(run_id=graph)["nodes"] if node["nodeId"] == "work"
    )
    assert pending["state"] == "pending"
    assert pending["deadlineMs"] is None
    assert pending["timeoutMs"] == 1000

    clock[0] = 5000
    complete(svc, graph, "upstream")
    requested = next(
        node for node in svc.status(run_id=graph)["nodes"] if node["nodeId"] == "work"
    )
    assert requested["state"] == "requested"
    assert requested["deadlineMs"] == 6000
    assert requested["timeoutMs"] == 1000
    store = PacGraphStore(svc.database, read_only=True)
    try:
        assert store.node(graph, "_deadline.work").owner == "user:escalation"
    finally:
        store.close()

    clock[0] = 6000
    svc._tick()
    assert svc.status(run_id=graph)["state"] == "running"
    clock[0] = 6001
    svc._tick()
    assert svc.status(run_id=graph)["state"] == "failed"


def test_unrequested_timeout_does_not_expire_while_dependencies_wait(service):
    svc, _, clock = service
    graph = start(
        svc,
        [
            {"id": "upstream", "task": "upstream", "owner": AGENT},
            {
                "id": "work",
                "task": "work",
                "owner": AGENT,
                "after": ["upstream"],
                "timeout": "1s",
            },
        ],
    )

    clock[0] = 5000
    svc._tick()
    snapshot = svc.status(run_id=graph)
    pending = next(node for node in snapshot["nodes"] if node["nodeId"] == "work")
    assert snapshot["state"] == "running"
    assert pending["state"] == "pending"
    assert pending["deadlineMs"] is None
    store = PacGraphStore(svc.database, read_only=True)
    try:
        assert store.node(graph, "_deadline.work") is None
    finally:
        store.close()


def test_explicit_deadline_is_absolute_before_and_after_request(service):
    svc, _, clock = service
    graph = start(
        svc,
        [
            {"id": "upstream", "task": "upstream", "owner": AGENT},
            {
                "id": "work",
                "task": "work",
                "owner": AGENT,
                "after": ["upstream"],
                "deadline_ms": 7000,
            },
        ],
    )
    svc._tick()

    pending = next(
        node for node in svc.status(run_id=graph)["nodes"] if node["nodeId"] == "work"
    )
    assert pending["deadlineMs"] == 7000
    store = PacGraphStore(svc.database, read_only=True)
    try:
        assert store.node(graph, "_deadline.work").deadline_ms == 7000
    finally:
        store.close()

    clock[0] = 5000
    complete(svc, graph, "upstream")
    requested = next(
        node for node in svc.status(run_id=graph)["nodes"] if node["nodeId"] == "work"
    )
    assert requested["state"] == "requested"
    assert requested["deadlineMs"] == 7000


def test_rerequest_keeps_the_first_request_deadline(service):
    svc, _, clock = service
    graph = start(
        svc,
        [
            {"id": "upstream", "task": "upstream", "owner": AGENT},
            {
                "id": "work",
                "task": "work",
                "owner": AGENT,
                "after": ["upstream"],
                "timeout": "1s",
            },
        ],
    )
    svc._tick()

    clock[0] = 1200
    complete(svc, graph, "upstream")
    first = next(
        node for node in svc.status(run_id=graph)["nodes"] if node["nodeId"] == "work"
    )
    assert first["deadlineMs"] == 2200

    clock[0] = 1500
    store = PacGraphStore(svc.database)
    try:
        PacReactor(store, clock=svc.clock).reset_flag(
            graph, "upstream", actor=AGENT
        )
    finally:
        store.close()
    svc._tick()
    complete(svc, graph, "upstream")
    second = next(
        node for node in svc.status(run_id=graph)["nodes"] if node["nodeId"] == "work"
    )
    assert second["requestId"] != first["requestId"]
    assert second["deadlineMs"] == 2200
    store = PacGraphStore(svc.database, read_only=True)
    try:
        generation = store._db.execute(
            "SELECT generation FROM workflow_nodes WHERE graph_id=? AND node_id='work'",
            (graph,),
        ).fetchone()[0]
        assert generation == 2
    finally:
        store.close()


def test_stale_completed_node_still_has_a_fixed_deadline(service):
    svc, _, clock = service
    graph = start(
        svc,
        [
            {"id": "a", "task": "a", "owner": AGENT},
            {"id": "b", "task": "b", "owner": AGENT, "after": ["a"]},
            {"id": "c", "task": "c", "owner": AGENT, "after": ["b"]},
        ],
        defaults={"timeout": "1s"},
    )
    svc._tick()
    complete(svc, graph, "a")
    complete(svc, graph, "b")
    store = PacGraphStore(svc.database)
    try:
        PacReactor(store, clock=svc.clock).reset_flag(graph, "a", actor=AGENT)
    finally:
        store.close()
    clock[0] = 2001
    svc._tick()
    assert svc.status(run_id=graph)["state"] == "failed"
    assert (
        next(n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "b")[
            "state"
        ]
        == "cancelled"
    )


def test_slow_notification_io_cannot_delay_a_graph_deadline(tmp_path):
    import threading
    import time

    entered, release = threading.Event(), threading.Event()
    clock = [1000]

    class SlowSender:
        def send(self, **kwargs):
            entered.set()
            assert release.wait(3)
            return kwargs["idempotency_key"]

    svc = GraphWorkflowService(
        state_dir=tmp_path,
        owner="test",
        machine="machine",
        sender=SlowSender(),
        clock_ms=lambda: clock[0],
    )
    try:
        graph = start(
            svc, [{"id": "work", "owner": AGENT, "task": "work", "timeout": "1s"}]
        )
        assert entered.wait(2)
        clock[0] = 2001
        svc.submit_timer(clock[0])
        deadline = time.monotonic() + 2
        while svc.status(run_id=graph)["state"] != "failed":
            assert time.monotonic() < deadline, "deadline waited for notification IO"
            time.sleep(0.005)
        assert not release.is_set()
    finally:
        release.set()
        assert svc.close()
