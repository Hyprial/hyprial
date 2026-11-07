import time

import pytest
import yaml

from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.biz.impl.routine.execution.pac_dispatch import PacRoutineDispatch
from hyprial.biz.impl.routine.contracts.schema import RoutineSchemaError, load_routine_text
from hyprial.biz.impl.routine.execution.service import RoutineService

OWNER = "user:test"
ACTOR = "agent:test:machine:coordinator"


def definition(**changes):
    return yaml.safe_dump(
        {
            "version": 2,
            "mode": "scheduled",
            "name": "coordinate",
            "schedule": {"interval": "60s"},
            "actor": ACTOR,
            "task": "Check progress and dispatch necessary workflows.",
            "on_task_timeout": {"action": "escalate", "escalate_to": OWNER},
            **changes,
        }
    )


class Clock:
    now = 1000

    def __call__(self):
        return self.now


class Plane:
    def __init__(self):
        self.messages = {}

    def __call__(self, **kwargs):
        self.messages.setdefault(kwargs["conversation_id"], kwargs)
        return True


class Alarm:
    def escalate(self, **kwargs):
        pass


def wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def service(state, clock, plane):
    state.mkdir(exist_ok=True)

    def forbidden_query(_filter):
        raise AssertionError("scheduled mode must not query a task source")

    return RoutineService(
        pac=PacRoutineDispatch(
            state_dir=state,
            deliver=plane,
            clock_ms=clock,
            resolve_principal=lambda principal: principal,
        ),
        alarm=Alarm(),
        state_dir=state,
        clock_ms=clock,
        source_query=forbidden_query,
    )


def graphs(state):
    store = PacGraphStore(default_database_path(state))
    try:
        return [
            dict(r)
            for r in store._db.execute(
                "SELECT * FROM graphs ORDER BY created_at,graph_id"
            )
        ]
    finally:
        store.close()


def finish(state, graph_id, clock):
    store = PacGraphStore(default_database_path(state))
    try:
        PacReactor(store, clock=clock).set_flag(
            graph_id, "work", actor=ACTOR, reason_ref="result:coordination"
        )
    finally:
        store.close()


def test_explicit_scheduled_mode_and_serial_policy():
    spec = load_routine_text(definition())
    assert spec.mode == "scheduled"
    assert spec.role == "dispatch"
    assert load_routine_text(definition(role="execute")).role == "execute"
    assert spec.limits.max_in_flight == 1
    assert spec.actor == ACTOR
    assert spec.produces is None
    assert spec.launch is None
    for changes in (
        {"mode": "unknown"},
        {"mode": "source"},
        {"source": {"kind": "taskwarrior", "filter": "all"}},
        {"limits": {"max_in_flight": 2}},
        {"launch": {"tier": "strong"}},
    ):
        with pytest.raises(RoutineSchemaError):
            load_routine_text(definition(**changes))


def test_each_period_has_a_new_graph_and_busy_period_is_skipped(tmp_path):
    clock, plane = Clock(), Plane()
    svc = service(tmp_path, clock, plane)
    try:
        svc.add(yaml_text=definition(), owner=OWNER)
        clock.now = 61000
        svc.submit_timer()
        wait(lambda: len(svc.status(name="coordinate")["inFlight"]) == 1)
        first = graphs(tmp_path)[0]["graph_id"]
        clock.now = 121000
        svc.submit_timer()
        wait(
            lambda: any(
                e["reason"] == "previous-occurrence-active"
                for e in svc.status(name="coordinate")["scheduleEvents"]
            )
        )
        assert len(graphs(tmp_path)) == 1
        finish(tmp_path, first, clock)
        clock.now = 181000
        svc.submit_timer()
        wait(lambda: len(graphs(tmp_path)) == 2)
        wait(lambda: len(svc.status(name="coordinate")["inFlight"]) == 1)
        assert len(plane.messages) == 2
        keys = {g["operation_key"] for g in graphs(tmp_path)}
        assert keys == {
            "routine:coordinate:scheduled:61000",
            "routine:coordinate:scheduled:181000",
        }
        assert svc.status(name="coordinate")["mode"] == "scheduled"
    finally:
        svc.close()


def test_restart_skips_missed_slots_and_waits_for_next_normal_slot(tmp_path):
    clock, plane = Clock(), Plane()
    svc = service(tmp_path, clock, plane)
    svc.add(yaml_text=definition(), owner=OWNER)
    registration = svc.status(name="coordinate")["registrationId"]
    svc.close()
    clock.now = 200000
    restarted = service(tmp_path, clock, plane)
    try:
        status = restarted.status(name="coordinate")
        assert status["registrationId"] == registration
        assert status["nextDueMs"] == 241000
        assert any(
            e["reason"] == "daemon-downtime" and e["count"] == 3
            for e in status["scheduleEvents"]
        )
        restarted.submit_timer()
        assert plane.messages == {}
        clock.now = 241000
        restarted.submit_timer()
        wait(lambda: len(restarted.status(name="coordinate")["inFlight"]) == 1)
        assert len(graphs(tmp_path)) == 1
        assert (
            graphs(tmp_path)[0]["operation_key"]
            == "routine:coordinate:scheduled:241000"
        )
    finally:
        restarted.close()


def test_pause_resume_preserves_phase_and_new_registration_has_new_identity(tmp_path):
    clock, plane = Clock(), Plane()
    svc = service(tmp_path, clock, plane)
    try:
        svc.add(yaml_text=definition(), owner=OWNER)
        old = svc.status(name="coordinate")["registrationId"]
        svc.pause(name="coordinate")
        clock.now = 120000
        svc.resume(name="coordinate")
        assert svc.status(name="coordinate")["nextDueMs"] == 121000
        svc.remove(name="coordinate")
        svc.add(yaml_text=definition(), owner=OWNER)
        assert svc.status(name="coordinate")["registrationId"] != old
    finally:
        svc.close()


def test_production_routine_bridge_uses_fenced_workflow_requests(tmp_path):
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

    clock = Clock()
    messages = []

    class Sender:
        def send(self, **message):
            messages.append(message)
            return message["idempotency_key"]

    workflow = GraphWorkflowService(
        state_dir=tmp_path,
        owner="test",
        machine="machine",
        sender=Sender(),
        clock_ms=clock,
        start_thread=False,
    )
    routine = RoutineService(
        pac=PacRoutineDispatch(
            state_dir=tmp_path,
            deliver=lambda **_: pytest.fail("used old dispatch"),
            clock_ms=clock,
            resolve_principal=lambda x: x,
            workflow=workflow,
        ),
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=clock,
    )
    try:
        routine.add(yaml_text=definition(), owner=OWNER)
        clock.now = 61000
        routine.submit_timer()
        wait(lambda: len(routine.status(name="coordinate")["inFlight"]) == 1)
        graph = routine.status(name="coordinate")["inFlight"][0]["runId"]
        workflow._tick()
        assert len(messages) == 1
        assert "hyprial workflow complete" in messages[0]["text"]
        node = workflow.status(run_id=graph)["nodes"][0]
        store = PacGraphStore(workflow.database)
        try:
            PacReactor(store, clock=clock).set_flag(
                graph,
                "work",
                actor=ACTOR,
                expected_request=node["requestId"],
                reason_ref="result:coordination",
            )
        finally:
            store.close()
        workflow._tick()
        assert workflow.status(run_id=graph)["state"] == "completed"
        clock.now = 121000
        routine.submit_timer()
        wait(lambda: len(workflow.list()["runs"]) == 2)
        assert workflow.status(run_id=graph)["state"] == "completed"
    finally:
        routine.close()
        assert workflow.close()


def test_managed_source_replay_uses_uuid_when_idle_description_changes(tmp_path):
    from hyprial.daemon.impl.pac.graphs.reactor import NullSender
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
    from hyprial.identity import PacError

    workflow = GraphWorkflowService(
        state_dir=tmp_path,
        owner="test",
        machine="machine",
        sender=NullSender(),
        start_thread=False,
    )
    bridge = PacRoutineDispatch(
        state_dir=tmp_path,
        workflow=workflow,
        deliver=lambda **_: pytest.fail("replayed a source delivery"),
        clock_ms=lambda: 1000,
        resolve_principal=lambda value: value,
    )
    args = dict(
        routine_name="source",
        task_uuid="same-source-uuid",
        task_text="idle for 1m",
        target="agent:test:machine:coordinator",
        escalate_to="user:test",
        timeout_seconds=3600,
        sender="user:test",
    )
    try:
        first = bridge.start_idempotent(**args)
        graph = first["graphId"]
        assert bridge.start_idempotent(**{**args, "task_text": "idle for 2m"}) == first
        workflow._tick()
        node = workflow.status(run_id=graph)["nodes"][0]
        store = PacGraphStore(workflow.database)
        try:
            PacReactor(store).set_flag(
                graph,
                node["nodeId"],
                actor=node["owner"],
                expected_request=node["requestId"],
                reason_ref="evidence:source-done",
            )
        finally:
            store.close()
        workflow._tick()
        bridge._resolve_principal = lambda _: pytest.fail(
            "readmitted a settled source task"
        )
        assert bridge.start_idempotent(**{**args, "task_text": "idle for 3m"}) == {
            "graphId": graph,
            "state": "done",
        }
        with pytest.raises(PacError) as error:
            bridge.start_idempotent(**{**args, "sender": "user:other"})
        assert error.value.code == "PAC_OPERATION_KEY_CONFLICT"
    finally:
        workflow.close()
