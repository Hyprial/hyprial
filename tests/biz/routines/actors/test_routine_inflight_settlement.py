from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.biz.impl.routine.execution.service import RoutineService, RoutineServiceError
from hyprial.biz.impl.routine.execution.source import SourceTask
from hyprial.biz.impl.routine.contracts.ports import RoutineTimerCompleted
from hyprial.daemon.impl.application.actors.routines import _RoutinesMixin
from tests.biz.routines.actors.test_routine_actor_registry import (
    Alarm,
    Clock,
    IdempotentPac,
    SCHEDULED,
    VALID,
    wait_for,
)


@pytest.fixture
def case(tmp_path):
    clock, pac = Clock(), IdempotentPac()
    tasks = [SourceTask("task-a", "Check work", ("route:self",))]
    events = []
    svc = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _: list(tasks),
        event_sink=events.append,
    )
    result = SimpleNamespace(svc=svc, clock=clock, pac=pac, tasks=tasks, events=events)
    try:
        yield result
    finally:
        result.svc.close()


def start(case, definition=SCHEDULED):
    case.svc.add(yaml_text=definition, owner="agent:o:n:duty")
    case.clock.now_ms = 60_000
    case.svc.submit_timer()
    wait_for(lambda: len(case.svc.status(name="actor-routine")["inFlight"]) == len(case.tasks))
    wait_for(lambda: case.svc._projection.cycle_for_routine("actor-routine") is None)
    return case.svc.status(name="actor-routine")["inFlight"]


def listed(case):
    app = SimpleNamespace(
        _routine_service=case.svc, _workflow_service=None, owner="o"
    )
    return _RoutinesMixin._ipc_routine_list(app, {"all": True}, "agent:o:n:duty")["routines"][0]


def observed_tick(case):
    before = sum(isinstance(event, RoutineTimerCompleted) for event in case.events)
    case.svc.submit_timer()
    wait_for(lambda: sum(isinstance(event, RoutineTimerCompleted) for event in case.events) > before)


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("definition", [SCHEDULED, VALID], ids=["scheduled", "source"])
def test_completed_cycle_clears_inflight_and_allows_set(case, paused, definition):
    record = start(case, definition)[0]
    if paused:
        case.svc.pause(name="actor-routine")
    case.pac.statuses[record["runId"]]["state"] = "done"
    observed_tick(case)
    wait_for(lambda: listed(case)["lastOutcome"] == "done")
    status = listed(case)
    assert status["lastOutcome"] == "done"
    assert status["inFlight"] == []
    wait_for(lambda: case.svc._projection.cycle_for_routine("actor-routine") is None)
    result = case.svc.set(name="actor-routine", yaml_text=definition)
    assert result["enabled"] is (not paused)
    assert len(case.pac.created) == 1


def test_running_graph_still_blocks_set_while_add_succeeds(case):
    record = start(case)[0]
    case.svc.pause(name="actor-routine")
    next_due = case.svc.status(name="actor-routine")["nextDueMs"]
    observed_tick(case)
    wait_for(lambda: case.svc._projection.cycle_for_routine("actor-routine") is None)
    assert case.svc.status(name="actor-routine")["enabled"] is False
    assert case.svc.status(name="actor-routine")["nextDueMs"] == next_due
    added = case.svc.add(
        yaml_text=SCHEDULED.replace("actor-routine", "another-routine"),
        owner="agent:o:n:duty",
    )
    assert added["name"] == "another-routine"
    with pytest.raises(RoutineServiceError) as rejected:
        case.svc.set(name="actor-routine", yaml_text=SCHEDULED)
    assert rejected.value.code == "ROUTINE_BUSY"
    assert case.svc.status(name="actor-routine")["inFlight"] == [record]
    case.pac.statuses[record["runId"]]["state"] = "done"
    case.svc.submit_timer()
    wait_for(lambda: not case.svc.status(name="actor-routine")["inFlight"])
    wait_for(lambda: case.svc._projection.cycle_for_routine("actor-routine") is None)
    assert case.svc.set(name="actor-routine", yaml_text=SCHEDULED)["name"] == "actor-routine"


def test_settling_one_source_task_keeps_other_running_record(case):
    case.tasks.append(SourceTask("task-b", "Other work", ("route:self",)))
    first, second = start(case, VALID)
    case.svc.pause(name="actor-routine")
    case.pac.statuses[first["runId"]]["state"] = "done"
    case.svc.submit_timer()
    wait_for(lambda: listed(case)["lastOutcome"] == "done")
    assert case.svc.status(name="actor-routine")["inFlight"] == [second]
    assert case.pac.closed == [first["runId"]]
    with pytest.raises(RoutineServiceError) as rejected:
        case.svc.set(name="actor-routine", yaml_text=VALID)
    assert rejected.value.code == "ROUTINE_BUSY"


def test_status_only_poll_preserves_consecutive_source_errors(case, monkeypatch):
    from hyprial.biz.impl.routine.execution.source import SourceTransientError

    start(case, VALID)

    def unavailable(_):
        raise SourceTransientError("isolated source outage")

    monkeypatch.setattr(case.svc, "_source_query", unavailable)
    case.clock.now_ms = 120_000
    observed_tick(case)
    wait_for(lambda: case.svc.status(name="actor-routine")["sourceErrorStreak"] == 1)
    observed_tick(case)
    wait_for(lambda: case.svc._projection.cycle_for_routine("actor-routine") is None)
    assert case.svc.status(name="actor-routine")["sourceErrorStreak"] == 1


def test_status_read_error_keeps_unconfirmed_graph_inflight(case, monkeypatch):
    record = start(case)[0]
    case.svc.pause(name="actor-routine")

    def unavailable(**_):
        raise RuntimeError("isolated graph status unavailable")

    monkeypatch.setattr(case.pac, "status", unavailable)
    observed_tick(case)
    wait_for(lambda: case.svc._projection.cycle_for_routine("actor-routine") is None)
    assert case.svc.status(name="actor-routine")["inFlight"] == [record]
    assert case.pac.closed == []


def test_paused_poll_does_not_repeat_circuit_breaker_alarm(case):
    case.tasks.append(SourceTask("task-b", "Other work", ("route:self",)))
    case.tasks.append(SourceTask("task-c", "Retained work", ("route:self",)))
    first, second, third = start(case, VALID.replace("window_runs: 5", "window_runs: 2"))
    case.pac.statuses[first["runId"]]["state"] = "escalated"
    case.pac.statuses[second["runId"]]["state"] = "escalated"
    observed_tick(case)
    wait_for(lambda: case.svc.status(name="actor-routine")["enabled"] is False)
    wait_for(lambda: len(case.svc._alarm.calls) == 1)
    observed_tick(case)
    wait_for(lambda: case.svc._projection.cycle_for_routine("actor-routine") is None)
    assert len(case.svc._alarm.calls) == 1
    assert case.svc.status(name="actor-routine")["inFlight"] == [third]


@pytest.mark.parametrize("terminal", ["done", "escalated"])
def test_settlement_survives_restart_with_paused_dirty_record(case, tmp_path, terminal):
    record = start(case)[0]
    case.svc.pause(name="actor-routine")
    case.svc.close()
    case.pac.statuses[record["runId"]]["state"] = terminal
    case.svc = RoutineService(
        pac=case.pac, alarm=Alarm(), state_dir=tmp_path,
        clock_ms=lambda: case.clock.now_ms,
        event_sink=case.events.append,
    )
    case.svc.submit_timer()
    wait_for(lambda: not case.svc.status(name="actor-routine")["inFlight"])
    status = case.svc.status(name="actor-routine")
    assert status["outcomes"] == [terminal]
    assert status["enabled"] is False
    assert len(case.pac.created) == 1


@pytest.mark.parametrize("exit_path", ["completed", "failed", "timeout", "cancelled"])
def test_real_pac_exit_paths_clear_paused_inflight(tmp_path, exit_path):
    from hyprial.biz.impl.routine.execution.pac_dispatch import PacRoutineDispatch
    from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

    clock = Clock()
    actor = "agent:o:n:duty"

    class Sender:
        def send(self, **message):
            return message["idempotency_key"]

    workflow = GraphWorkflowService(
        state_dir=tmp_path, owner="o", machine="n", sender=Sender(),
        clock_ms=lambda: clock.now_ms, start_thread=False,
    )
    pac = PacRoutineDispatch(
        state_dir=tmp_path, workflow=workflow, deliver=lambda **_: True,
        clock_ms=lambda: clock.now_ms, resolve_principal=lambda p: p,
    )
    svc = RoutineService(
        pac=pac, alarm=Alarm(), state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
    )
    try:
        svc.add(yaml_text=SCHEDULED, owner=actor)
        clock.now_ms = 60_000
        svc.submit_timer()
        wait_for(lambda: bool(svc.status(name="actor-routine")["inFlight"]))
        wait_for(lambda: svc._projection.cycle_for_routine("actor-routine") is None)
        graph = svc.status(name="actor-routine")["inFlight"][0]["runId"]
        workflow._tick()
        node = workflow.status(run_id=graph)["nodes"][0]
        svc.pause(name="actor-routine")
        if exit_path == "completed":
            store = PacGraphStore(workflow.database)
            try:
                PacReactor(store, clock=lambda: clock.now_ms).set_flag(
                    graph, "work", actor=actor, expected_request=node["requestId"],
                    reason_ref="result:isolated-completion",
                )
            finally:
                store.close()
        elif exit_path == "failed":
            workflow.fail(
                graph_id=graph, node_id="work", actor=actor,
                request_id=node["requestId"], reason_ref="result:isolated-failure",
            )
        elif exit_path == "timeout":
            clock.now_ms = node["deadlineMs"] + 1
        else:
            workflow.cancel(run_id=graph, actor=actor, reason_ref="result:isolated-abort")
        workflow._tick()
        expected = "completed" if exit_path == "completed" else "cancelled" if exit_path == "cancelled" else "failed"
        assert workflow.status(run_id=graph)["state"] == expected
        svc.submit_timer()
        wait_for(lambda: not svc.status(name="actor-routine")["inFlight"])
        status = svc.status(name="actor-routine")
        assert status["outcomes"] == ["done" if exit_path == "completed" else "escalated"]
        assert status["enabled"] is False
        wait_for(lambda: svc._projection.cycle_for_routine("actor-routine") is None)
        assert svc.set(name="actor-routine", yaml_text=SCHEDULED)["enabled"] is False
    finally:
        svc.close()
        assert workflow.close()
