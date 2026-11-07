import pytest
import yaml

from hyprial.daemon.impl.pac.actors.coordinator import (
    RuntimeObservation,
)
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

USER = "user:test"
AGENT = "agent:test:machine:existing"


class Sender:
    def __init__(self):
        self.messages = {}

    def send(self, **kwargs):
        key = kwargs["idempotency_key"]
        self.messages.setdefault(key, kwargs)
        return key


class Runtime:
    def __init__(self):
        self.running = {}
        self.starts = []
        self.stops = []

    def observe(self, actor_name):
        return self.running.get(actor_name, RuntimeObservation(False))

    def start(self, actor_name, launch, *, operation_id, identity_marker):
        self.starts.append(actor_name)
        observation = RuntimeObservation(
            True, identity_marker, launch.harness, operation_id
        )
        self.running[actor_name] = observation
        return observation

    def stop(self, actor_name, launch, *, operation_id, identity_marker):
        assert self.running[actor_name].identity_marker == identity_marker
        self.stops.append(actor_name)
        del self.running[actor_name]
        return RuntimeObservation(False)


@pytest.fixture
def service(tmp_path):
    sender = Sender()
    clock = [1000]
    service = GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=sender,
        clock_ms=lambda: clock[0],
        start_thread=False,
    )
    yield service, sender, clock
    assert service.close()


def start(service, nodes, *, policy="terminate", key="task", **extra):
    return service.start(
        yaml_text=yaml.safe_dump(
            {
                "version": 2,
                "name": "test",
                "nodes": nodes,
                "on_failure": policy,
                **extra,
            }
        ),
        sender=USER,
        operation_key=key,
    )["graphId"]


def complete(service, graph, node):
    status = service.status(run_id=graph)
    row = next(n for n in status["nodes"] if n["nodeId"] == node)
    store = PacGraphStore(service.database)
    try:
        PacReactor(store, clock=service.clock).set_flag(
            graph,
            node,
            actor=row["owner"],
            expected_request=row["requestId"],
            reason_ref="result:test",
        )
    finally:
        store.close()
    service._tick()


















































def bound_message(service):
    store = PacGraphStore(service.database, read_only=True)
    try:
        return store._db.execute(
            "SELECT message_id FROM workflow_deliveries ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        store.close()




















# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'AGENT',
    'Runtime',
    'Sender',
    'USER',
    'bound_message',
    'complete',
    'service',
    'start',
]
