"""Reproduction: a settled graph must not keep blocking ``routine set``.

Report: ``docs/notes/routine-inflight-settle-2026-11-04.md``.

The routine's in_flight row is written by the ``pac.start`` completion and is
only ever removed downstream of a ``pac.status`` completion, and those
effects are minted only when a new routine cycle starts.  Between polls a
graph whose ``work`` node owner already flagged it therefore still counted as
an active cycle, and ``routine set`` answered ROUTINE_BUSY for a run that is
over.  A graph that is still running must keep answering ROUTINE_BUSY.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hyprial.biz.impl.routine.execution.service import RoutineService, RoutineServiceError


SCHEDULED = """
version: 2
mode: scheduled
name: settle-routine
schedule: {interval: 60s}
actor: agent:o:n:duty
task: Check progress and dispatch what is needed.
on_task_timeout: {action: escalate, escalate_to: "user:allen"}
"""


class Clock:
    now_ms = 0


class Alarm:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def escalate(
        self,
        *,
        to: str,
        text: str,
        reason: str | None = None,
        conversation_id: str = "workflow",
    ) -> None:
        self.calls.append((to, text))


class Pac:
    """A PAC port whose graphs settle only when the test says so."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.statuses: dict[str, dict[str, object]] = {}
        self.closed: list[str] = []

    def start_idempotent(
        self,
        *,
        routine_name: str,
        task_uuid: str,
        task_text: str,
        target: str,
        escalate_to: str,
        timeout_seconds: float,
        sender: str,
        role: str = "dispatch",
        occurrence_slot_ms: int | None = None,
        rearm_after_created_at_ms: int = 0,
    ) -> dict[str, object]:
        del task_text, escalate_to, timeout_seconds, sender, role, rearm_after_created_at_ms
        graph_id = f"routine-{routine_name}-{len(self.created):08x}"
        self.created.append(graph_id)
        self.statuses[graph_id] = {"state": "running", "target": target}
        del task_uuid, occurrence_slot_ms
        return {"graphId": graph_id, "state": "running"}

    def status(self, *, graph_id: str) -> dict[str, object]:
        return self.statuses[graph_id]

    def close(self, *, graph_id: str, actor: str) -> None:
        del actor
        self.closed.append(graph_id)

    def rearm_boundary(self, *, routine_name: str) -> int:
        del routine_name
        return 0


def wait_for(predicate, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_settled_graph_no_cycle_is_still_reported_in_flight(tmp_path: Path) -> None:
    clock = Clock()
    pac = Pac()
    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: pytest.fail("scheduled mode queries no source"),
    )
    try:
        service.add(yaml_text=SCHEDULED, owner="agent:o:n:duty")
        clock.now_ms = 60_000
        service.submit_timer()
        wait_for(lambda: bool(service.status(name="settle-routine")["inFlight"]))
        wait_for(
            lambda: service._projection.cycle_for_routine("settle-routine") is None
        )
        [graph_id] = pac.created
        in_flight = service.status(name="settle-routine")["inFlight"]
        assert in_flight == [
            {"taskUuid": "scheduled:60000", "runId": graph_id, "target": "agent:o:n:duty"}
        ]

        pac.statuses[graph_id]["state"] = "done"

        service.set(
            name="settle-routine",
            yaml_text=SCHEDULED.replace("what is needed", "what is pending"),
        )
        settled = service.status(name="settle-routine")
        assert settled["inFlight"] == []
        assert settled["outcomes"] == ["done"]
    finally:
        service.close()


def test_running_graph_still_answers_routine_busy(tmp_path: Path) -> None:
    clock = Clock()
    pac = Pac()
    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: pytest.fail("scheduled mode queries no source"),
    )
    try:
        service.add(yaml_text=SCHEDULED, owner="agent:o:n:duty")
        clock.now_ms = 60_000
        service.submit_timer()
        wait_for(lambda: bool(service.status(name="settle-routine")["inFlight"]))
        wait_for(
            lambda: service._projection.cycle_for_routine("settle-routine") is None
        )
        [graph_id] = pac.created

        with pytest.raises(RoutineServiceError) as busy:
            service.set(
                name="settle-routine",
                yaml_text=SCHEDULED.replace("what is needed", "what is pending"),
            )
        assert busy.value.code == "ROUTINE_BUSY"
        kept = service.status(name="settle-routine")
        assert kept["inFlight"] == [
            {"taskUuid": "scheduled:60000", "runId": graph_id, "target": "agent:o:n:duty"}
        ]
        assert kept["outcomes"] == []
    finally:
        service.close()


def _in_flight_service(tmp_path: Path, pac: Pac) -> tuple[RoutineService, str]:
    clock = Clock()
    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: pytest.fail("scheduled mode queries no source"),
    )
    service.add(yaml_text=SCHEDULED, owner="agent:o:n:duty")
    clock.now_ms = 60_000
    service.submit_timer()
    wait_for(lambda: bool(service.status(name="settle-routine")["inFlight"]))
    wait_for(lambda: service._projection.cycle_for_routine("settle-routine") is None)
    [graph_id] = pac.created
    return service, graph_id


def test_escalated_graph_is_settled_and_recorded(tmp_path: Path) -> None:
    pac = Pac()
    service, graph_id = _in_flight_service(tmp_path, pac)
    try:
        pac.statuses[graph_id]["state"] = "escalated"

        service.set(
            name="settle-routine",
            yaml_text=SCHEDULED.replace("what is needed", "what is pending"),
        )
        settled = service.status(name="settle-routine")
        assert settled["inFlight"] == []
        assert settled["outcomes"] == ["escalated"]
    finally:
        service.close()


@pytest.mark.parametrize("probe", ["unknown-state", "missing-state", "absent-graph"])
def test_unreadable_graph_state_is_never_settled(tmp_path: Path, probe: str) -> None:
    pac = Pac()
    service, graph_id = _in_flight_service(tmp_path, pac)
    try:
        if probe == "unknown-state":
            pac.statuses[graph_id]["state"] = "unknown"
        elif probe == "missing-state":
            del pac.statuses[graph_id]["state"]
        else:
            del pac.statuses[graph_id]

        with pytest.raises(RoutineServiceError) as busy:
            service.set(
                name="settle-routine",
                yaml_text=SCHEDULED.replace("what is needed", "what is pending"),
            )
        assert busy.value.code == "ROUTINE_BUSY"
        kept = service.status(name="settle-routine")
        assert kept["inFlight"] == [
            {"taskUuid": "scheduled:60000", "runId": graph_id, "target": "agent:o:n:duty"}
        ]
        assert kept["outcomes"] == []
    finally:
        service.close()


def test_paused_routine_with_settled_graph_accepts_set(tmp_path: Path) -> None:
    pac = Pac()
    service, graph_id = _in_flight_service(tmp_path, pac)
    try:
        service.pause(name="settle-routine")
        pac.statuses[graph_id]["state"] = "done"

        service.set(
            name="settle-routine",
            yaml_text=SCHEDULED.replace("what is needed", "what is pending"),
        )
        settled = service.status(name="settle-routine")
        assert settled["inFlight"] == []
        assert settled["outcomes"] == ["done"]
    finally:
        service.close()
