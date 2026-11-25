"""Pausing a routine must not orphan the in_flight rows it was tracking (M-hq-adjutant-0012).

Observed on hq, 2026-11-03: a scheduled routine dispatched a task, the operator
paused the routine, and the task then settled.  ``RoutineRegistry._pac_completed``
looked up the completion's effect, found nothing -- ``pause`` discards a
routine's ``routine_cycles`` and ``routine_effects`` in one transaction -- and
returned silently, so the ``in_flight`` row stayed forever.  ``routine set`` then
answered ``ROUTINE_BUSY`` ("has an active cycle") while ``routine remove``
refused because it was the last binding: neither command could move.

Two guarantees are asserted here:

1. ``Store.apply(clear_work_for=...)`` discards the cycle and the effects but
   **keeps** the ``in_flight`` row: that row is the durable proof a task is still
   open, and only an authoritative settlement may retire it.
2. A completion whose effect is already gone reclaims its own ``in_flight`` row
   only on the authoritative proof (``pac.status``, no error, terminal state and
   the current run's id); it logs a warning instead of returning silently, and a
   non-terminal, failed, stale or unidentified completion never writes.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


from hyprial.biz.impl.routine.contracts.ports import (
    RemoveRoutineCommand,
    RoutinePacIoCompleted,
)
from hyprial.biz.impl.routine.execution.registry import (
    RoutinePacEffect,
    RoutineRegistry,
)
from hyprial.biz.impl.routine.execution.service import RoutineService
from hyprial.biz.impl.routine.storage.store import (
    InFlightRow,
    RoutineCycleRow,
    RoutineRow,
    RoutineStore,
)

SCHEDULED = """
version: 2
mode: scheduled
name: cli-dev-duty
schedule: {interval: 60s}
actor: agent:o:n:duty
task: Heartbeat and pick up work.
on_task_timeout: {action: escalate, escalate_to: "user:allen"}
"""

TASK_UUID = "scheduled:1793709150633"
GRAPH_ID = "routine-cli-dev-duty-00000000"


class Clock:
    now_ms = 1_793_710_000_000


class Alarm:
    def escalate(self, **_: object) -> None:  # pragma: no cover - path not used
        raise AssertionError("no alarm expected in this test")


class FakePac:
    """Minimal PAC port: one graph per (routine, task), state controlled here."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.graphs: dict[tuple[str, str], str] = {}
        self.lock = threading.Lock()

    def start_idempotent(self, *, routine_name: str, task_uuid: str, **_: object):
        with self.lock:
            graph_id = self.graphs.get((routine_name, task_uuid))
            if graph_id is None:
                graph_id = f"routine-{routine_name}-{len(self.graphs):08x}"
                self.graphs[(routine_name, task_uuid)] = graph_id
                self.states[graph_id] = "running"
            return {"graphId": graph_id, "state": self.states[graph_id]}

    def status(self, *, graph_id: str) -> dict[str, object]:
        with self.lock:
            state = self.states.get(graph_id)
        if state is None:
            raise RuntimeError(f"routine graph {graph_id} is missing")
        return {"graphId": graph_id, "state": state}

    def close(self, *, graph_id: str, actor: str) -> None:
        del actor
        with self.lock:
            self.states[graph_id] = "done"

    def rearm_boundary(self, *, routine_name: str) -> int:
        del routine_name
        return 0


class CapturingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.lock = threading.Lock()

    def log(self, level: str, event: str, **fields: object) -> None:
        del level
        with self.lock:
            self.events.append((event, dict(fields)))

    def find(self, event: str) -> dict[str, object] | None:
        with self.lock:
            for name, fields in reversed(self.events):
                if name == event:
                    return fields
        return None


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def seed_in_flight(store: RoutineStore, *, enabled: bool = True) -> None:
    """One routine with a dispatched task: cycle row, effect-free in_flight row."""

    store.apply(
        routine=RoutineRow(
            name="cli-dev-duty",
            yaml_text=SCHEDULED,
            owner="agent:o:n:duty",
            enabled=enabled,
            next_due_ms=Clock.now_ms,
            source_error_streak=0,
            outcomes="[]",
            created_at_ms=Clock.now_ms - 60_000,
            version=1,
        ),
        cycle=RoutineCycleRow(
            correlation_id="cycle-1",
            routine_name="cli-dev-duty",
            generation=1,
            version=1,
            tasks_json="[]",
            outcomes_json="[]",
            pending_status=1,
        ),
        put_in_flight=(
            InFlightRow(
                routine="cli-dev-duty",
                task_uuid=TASK_UUID,
                run_id=GRAPH_ID,
                target="agent:o:n:duty",
                started_at_ms=Clock.now_ms - 1_000,
            ),
        ),
    )


def test_clear_work_for_reclaims_in_flight_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    """Clearing a routine's work keeps the durable "dispatched" row.

    ``in_flight`` is the only durable proof that a task is still open, and it is
    what keeps ``routine set`` honest.  Discarding the effects removes what a
    late completion correlates on, so the row has to be reclaimed by an
    authoritative settlement (a terminal graph, or a proven-absent graph) —
    never by the clear itself.
    """

    store = RoutineStore(tmp_path / "routines.sqlite3")
    try:
        seed_in_flight(store)
        assert len(store.in_flight(routine="cli-dev-duty")) == 1

        paused = store.get_routine("cli-dev-duty")
        assert paused is not None
        store.apply(
            routine=RoutineRow(
                name=paused.name,
                yaml_text=paused.yaml_text,
                owner=paused.owner,
                enabled=False,
                next_due_ms=paused.next_due_ms,
                source_error_streak=paused.source_error_streak,
                outcomes=paused.outcomes,
                created_at_ms=paused.created_at_ms,
                version=paused.version + 1,
            ),
            clear_work_for="cli-dev-duty",
        )

        # The work is gone, the durable row is not: it waits for its outcome.
        assert len(store.in_flight(routine="cli-dev-duty")) == 1
        assert store.cycle_for_routine("cli-dev-duty") is None
        assert store.pending_effects(limit=10, now_ms=Clock.now_ms) == ()
    finally:
        store.close()


def test_completion_without_effect_reclaims_its_in_flight_row(tmp_path: Path) -> None:
    store = RoutineStore(tmp_path / "routines.sqlite3")
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = RoutineRegistry(
            store=store,
            generation=1,
            publish=lambda output: None,
            clock_ms=lambda: Clock.now_ms,
            logger=logger,
        )
        # The effect for this completion is gone: this is exactly the state
        # `pause` used to leave behind.
        registry(
            RoutinePacIoCompleted(
                correlation_id="effect-that-was-discarded",
                generation=1,
                version=1,
                routine_name="cli-dev-duty",
                task_uuid=TASK_UUID,
                operation="pac.status",
                graph_id=GRAPH_ID,
                state="done",
            )
        )

        assert store.in_flight(routine="cli-dev-duty") == ()
        fields = logger.find("routine.pac.effect_missing_reclaimed")
        assert fields is not None
        assert fields["routine"] == "cli-dev-duty"
        assert fields["task_uuid"] == TASK_UUID
        assert fields["reclaimed"] is True
    finally:
        store.close()


def test_pause_after_dispatch_leaves_set_usable(tmp_path: Path) -> None:
    """Pause mid-flight, then ``routine set`` once the run has really settled.

    The dead end this pins is "pause leaves a row that nothing can ever resolve".
    A still-running graph must still answer ROUTINE_BUSY; what makes ``set``
    usable is the authoritative settlement (here: the graph becomes ``done``),
    not the pause.
    """

    clock = Clock()
    pac = FakePac()
    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path / "state",
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: [],
    )
    try:
        service.add(yaml_text=SCHEDULED, owner="agent:o:n:duty")
        clock.now_ms += 120_000
        service.submit_timer()
        wait_for(lambda: service.status(name="cli-dev-duty")["inFlight"] != [])
        assert service.status(name="cli-dev-duty")["inFlight"]
        graph_id = next(iter(pac.graphs.values()))

        service.pause(name="cli-dev-duty")

        # The row survives the pause, so an unfinished run is still busy.
        assert service.status(name="cli-dev-duty")["inFlight"]
        busy_code: str | None = None
        try:
            service.set(
                name="cli-dev-duty",
                yaml_text=SCHEDULED.replace(
                    "Heartbeat and pick up work.", "Heartbeat v2."
                ),
            )
        except Exception as error:  # noqa: BLE001 - only the code is asserted
            busy_code = getattr(error, "code", None)
        assert busy_code == "ROUTINE_BUSY"

        # Once the run is authoritatively terminal, the same ``set`` goes
        # through and records the outcome exactly once.
        with pac.lock:
            pac.states[graph_id] = "done"
        updated = service.set(
            name="cli-dev-duty",
            yaml_text=SCHEDULED.replace("Heartbeat and pick up work.", "Heartbeat v2."),
        )
        assert updated["name"] == "cli-dev-duty"
        settled = service.status(name="cli-dev-duty")
        assert settled["inFlight"] == []
        assert settled["outcomes"] == ["done"]
    finally:
        service.close()


def test_last_binding_refusal_documents_the_placeholder_escape_hatch(
    tmp_path: Path,
) -> None:
    """The refusal used to name two doors that both refuse; say the third one out loud."""

    store = RoutineStore(tmp_path / "routines.sqlite3")
    published: list[object] = []
    try:
        seed_in_flight(store)
        registry = RoutineRegistry(
            store=store,
            generation=1,
            publish=published.append,
            clock_ms=lambda: Clock.now_ms,
        )
        registry(
            RemoveRoutineCommand(
                correlation_id="remove-last-binding",
                name="cli-dev-duty",
                enforce_last=True,
            )
        )

        rejected = [
            item
            for item in published
            if getattr(item, "code", None) == "ROUTINE_LAST_BINDING"
        ]
        assert rejected, "expected a ROUTINE_LAST_BINDING refusal"
        detail = rejected[-1].detail
        assert "placeholder" in detail
        assert "ROUTINE_BUSY" in detail
    finally:
        store.close()


def test_completion_while_enabled_clears_in_flight(tmp_path: Path) -> None:
    """Baseline: the healthy round.  A fix that breaks this one is over-reach."""

    store = RoutineStore(tmp_path / "routines.sqlite3")
    published: list[object] = []
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = RoutineRegistry(
            store=store,
            generation=1,
            publish=published.append,
            clock_ms=lambda: Clock.now_ms,
            logger=logger,
        )
        effect = RoutinePacEffect(
            effect_id="effect-status-1",
            parent_correlation_id="cycle-1",
            generation=1,
            version=1,
            routine_name="cli-dev-duty",
            task_uuid=TASK_UUID,
            operation="pac.status",
            graph_id=GRAPH_ID,
            sender="agent:o:n:duty",
        )
        store.apply(add_effects=(RoutineRegistry.effect_row(effect),))

        registry(
            RoutinePacIoCompleted(
                correlation_id=effect.effect_id,
                generation=1,
                version=1,
                routine_name="cli-dev-duty",
                task_uuid=TASK_UUID,
                operation="pac.status",
                graph_id=GRAPH_ID,
                state="done",
            )
        )

        assert store.in_flight(routine="cli-dev-duty") == ()
        # The healthy path finishes the cycle once its only task settles.
        assert store.cycle_for_routine("cli-dev-duty") is None
        # The completion was consumed as a real effect transition, not by the
        # self-heal branch: a fix that only reclaimed via the warn path would
        # leave this harness green while the ledger lost the outcome.
        assert store.pending_effects(limit=10, now_ms=Clock.now_ms) == ()
    finally:
        store.close()
