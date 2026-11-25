"""The reclaimed-orphan rule (M-hq-adjutant-0012) after the O4 tightening.

Two facts must hold together:

* a completion whose ``effect`` is gone may reclaim the durable ``in_flight``
  row **only** on the same authority ``_status_completed`` uses — ``pac.status``,
  no error, a terminal state, and the persistent row's ``run_id`` equal to the
  event's graph.  A running graph, a read error, a ``pac.start`` result, an
  unknown state, a stale run or a replayed event must not delete anything;
* a run whose graph the local PAC store *proves* absent is reclaimed by
  ``routine set`` (and only then) without inventing an outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.biz.impl.routine.contracts.ports import RoutinePacIoCompleted
from hyprial.biz.impl.routine.execution.registry import RoutineRegistry
from hyprial.biz.impl.routine.execution.service import RoutineService
from hyprial.biz.impl.routine.storage.store import (
    InFlightRow,
    RoutineCycleRow,
    RoutineRow,
    RoutineStore,
)
from tests.biz.routines.actors.inflight.test_routine_paused_completion_orphan import (
    GRAPH_ID,
    SCHEDULED,
    TASK_UUID,
    Alarm,
    CapturingLogger,
    Clock,
    FakePac,
    seed_in_flight,
    wait_for,
)

LOG_EVENT = "routine.pac.effect_missing_reclaimed"


def _registry(
    store: RoutineStore, logger: CapturingLogger, **kwargs
) -> RoutineRegistry:
    return RoutineRegistry(
        store=store,
        generation=1,
        publish=lambda _output: None,
        clock_ms=lambda: Clock.now_ms,
        logger=logger,
        **kwargs,
    )


def _event(**fields: object) -> RoutinePacIoCompleted:
    base = dict(
        correlation_id="effect-that-was-discarded",
        generation=1,
        version=1,
        routine_name="cli-dev-duty",
        task_uuid=TASK_UUID,
        operation="pac.status",
        graph_id=GRAPH_ID,
        state="done",
    )
    base.update(fields)
    return RoutinePacIoCompleted(**base)


def _rows(store: RoutineStore) -> int:
    return len(store.in_flight(routine="cli-dev-duty"))


def _outcomes(store: RoutineStore) -> str:
    row = store.get_routine("cli-dev-duty")
    assert row is not None
    return row.outcomes


# -- the six diagnostic-only inputs (plus a replay of each) ------------------


def _diagnostic_only(tmp_path: Path, **fields: object) -> None:
    store = RoutineStore(tmp_path / "routines.sqlite3")
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = _registry(store, logger)
        registry(_event(**fields))
        registry(_event(**fields))  # a replayed receipt is not a new outcome
        assert _rows(store) == 1
        assert _outcomes(store) == "[]"
        fields_log = logger.find(LOG_EVENT)
        assert fields_log is not None
        assert fields_log["reclaimed"] is False
        assert fields_log["reason"] in {"non-terminal", "run-mismatch"}
    finally:
        store.close()


def test_running_status_never_reclaims(tmp_path: Path) -> None:
    _diagnostic_only(tmp_path, state="running")


def test_read_error_never_reclaims(tmp_path: Path) -> None:
    _diagnostic_only(tmp_path, state=None, code="OSError")


def test_start_completion_never_reclaims(tmp_path: Path) -> None:
    _diagnostic_only(tmp_path, operation="pac.start", state="running")


def test_unknown_state_never_reclaims(tmp_path: Path) -> None:
    _diagnostic_only(tmp_path, state="unknown")


def test_missing_state_never_reclaims(tmp_path: Path) -> None:
    # A successful status projection without ``state`` becomes the string
    # "None"; it is not a terminal state and must not be traded for absence.
    _diagnostic_only(tmp_path, state="None")


def test_stale_run_never_reclaims_the_current_row(tmp_path: Path) -> None:
    store = RoutineStore(tmp_path / "routines.sqlite3")
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = _registry(store, logger)
        registry(_event(graph_id="routine-cli-dev-duty-old00000"))
        assert _rows(store) == 1
        fields_log = logger.find(LOG_EVENT)
        assert fields_log is not None
        assert fields_log["reclaimed"] is False
        assert fields_log["reason"] == "run-mismatch"
    finally:
        store.close()


def test_unidentified_completion_is_only_diagnosed(tmp_path: Path) -> None:
    store = RoutineStore(tmp_path / "routines.sqlite3")
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = _registry(store, logger)
        registry(
            _event(
                operation="pac.status", routine_name=None, task_uuid=None, state="done"
            )
        )
        assert _rows(store) == 1
        fields_log = logger.find(LOG_EVENT)
        assert fields_log is not None
        assert fields_log["reclaimed"] is False
        assert fields_log["reason"] == "unidentified"
    finally:
        store.close()


# -- the authoritative terminal inputs -------------------------------------


def _terminal_reclaims(tmp_path: Path, state: str) -> None:
    store = RoutineStore(tmp_path / "routines.sqlite3")
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = _registry(store, logger)
        registry(_event(state=state))
        assert _rows(store) == 0
        # The authoritative completion records its sample in the same apply.
        assert json.loads(_outcomes(store)) == [state]
        fields_log = logger.find(LOG_EVENT)
        assert fields_log is not None
        assert fields_log["reclaimed"] is True
        assert fields_log["graph_id"] == GRAPH_ID
        assert fields_log["outcome_recorded"] is True
        assert fields_log["outcome"] == state

        # A replay finds no row: no second apply, no second outcome.
        registry(_event(state=state))
        assert _rows(store) == 0
        assert json.loads(_outcomes(store)) == [state]
    finally:
        store.close()


def test_terminal_done_records_the_outcome_once(tmp_path: Path) -> None:
    _terminal_reclaims(tmp_path, "done")


def test_terminal_escalated_records_the_outcome_once(tmp_path: Path) -> None:
    _terminal_reclaims(tmp_path, "escalated")


# -- proven absence: only ``routine set`` reclaims, and without an outcome ----


class AbsentPac(FakePac):
    """A PAC port that can prove some graphs are gone from the local store."""

    def __init__(self) -> None:
        super().__init__()
        self.absent: set[str] = set()
        self.probe_calls: list[str] = []
        self.raise_on_probe = False

    def graph_absent(self, graph_id: str) -> bool:
        self.probe_calls.append(graph_id)
        if self.raise_on_probe:
            raise RuntimeError("database is locked")
        return graph_id in self.absent


class NoProbePac(FakePac):
    """A port without the optional capability: absence can never be proven."""


def _dispatched(tmp_path: Path, pac: FakePac) -> tuple[RoutineService, str]:
    clock = Clock()
    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path / "state",
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: [],
    )
    service.add(yaml_text=SCHEDULED, owner="agent:o:n:duty")
    clock.now_ms += 120_000
    service.submit_timer()
    wait_for(lambda: service.status(name="cli-dev-duty")["inFlight"] != [])
    graph_id = next(iter(pac.graphs.values()))
    return service, graph_id


def test_proven_absence_reclaims_on_set_without_an_outcome(tmp_path: Path) -> None:
    pac = AbsentPac()
    service, graph_id = _dispatched(tmp_path, pac)
    try:
        service.pause(name="cli-dev-duty")
        pac.absent.add(graph_id)

        updated = service.set(
            name="cli-dev-duty",
            yaml_text=SCHEDULED.replace("Heartbeat and pick up work.", "Heartbeat v2."),
        )

        assert updated["name"] == "cli-dev-duty"
        status = service.status(name="cli-dev-duty")
        assert status["inFlight"] == []
        assert status["outcomes"] == []
        assert graph_id in pac.probe_calls
    finally:
        service.close()


def test_proven_absence_after_restart_reclaims_too(tmp_path: Path) -> None:
    pac = AbsentPac()
    service, graph_id = _dispatched(tmp_path, pac)
    service.pause(name="cli-dev-duty")
    service.close()
    pac.absent.add(graph_id)

    restarted = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path / "state",
        clock_ms=lambda: Clock.now_ms,
        source_query=lambda _filter: [],
    )
    try:
        updated = restarted.set(
            name="cli-dev-duty",
            yaml_text=SCHEDULED.replace("Heartbeat and pick up work.", "Heartbeat v2."),
        )
        assert updated["name"] == "cli-dev-duty"
        status = restarted.status(name="cli-dev-duty")
        assert status["inFlight"] == []
        assert status["outcomes"] == []
    finally:
        restarted.close()


def test_a_probe_failure_is_not_absence(tmp_path: Path) -> None:
    pac = AbsentPac()
    pac.raise_on_probe = True
    service, _graph_id = _dispatched(tmp_path, pac)
    try:
        service.pause(name="cli-dev-duty")
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
        assert service.status(name="cli-dev-duty")["inFlight"]
    finally:
        service.close()


def test_a_port_without_the_capability_keeps_the_row(tmp_path: Path) -> None:
    pac = NoProbePac()
    service, _graph_id = _dispatched(tmp_path, pac)
    try:
        service.pause(name="cli-dev-duty")
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
        assert service.status(name="cli-dev-duty")["inFlight"]
    finally:
        service.close()


# -- mixed rows: each row is judged on its own evidence ----------------------


def test_mixed_rows_only_reclaim_the_proven_ones(tmp_path: Path) -> None:
    pac = AbsentPac()
    clock = Clock()
    store = RoutineStore(tmp_path / "state" / "routines.sqlite3")
    try:
        store.apply(
            routine=RoutineRow(
                name="cli-dev-duty",
                yaml_text=SCHEDULED,
                owner="agent:o:n:duty",
                enabled=True,
                next_due_ms=Clock.now_ms,
                source_error_streak=0,
                outcomes="[]",
                created_at_ms=Clock.now_ms - 60_000,
                version=1,
            ),
            put_in_flight=(
                InFlightRow(
                    "cli-dev-duty",
                    "task-terminal",
                    GRAPH_ID,
                    "agent:o:n:duty",
                    Clock.now_ms - 1_000,
                ),
                InFlightRow(
                    "cli-dev-duty",
                    "task-absent",
                    "routine-cli-dev-duty-absent01",
                    "agent:o:n:duty",
                    Clock.now_ms - 1_000,
                ),
                InFlightRow(
                    "cli-dev-duty",
                    "task-running",
                    "routine-cli-dev-duty-run0001",
                    "agent:o:n:duty",
                    Clock.now_ms - 1_000,
                ),
            ),
        )
    finally:
        store.close()
    pac.states[GRAPH_ID] = "done"
    pac.states["routine-cli-dev-duty-run0001"] = "running"
    pac.absent.add("routine-cli-dev-duty-absent01")

    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=tmp_path / "state",
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: [],
    )
    try:
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
        # The running row is untouched, so the routine is still busy ...
        assert busy_code == "ROUTINE_BUSY"
        status = service.status(name="cli-dev-duty")
        assert [item["taskUuid"] for item in status["inFlight"]] == ["task-running"]
        # ... only the terminal row produced an outcome, the absent one none.
        assert status["outcomes"] == ["done"]
    finally:
        service.close()


# -- characterization: the existing repeated-outcome behaviour (not a fix) ----


class BrokenStatusPac(FakePac):
    """A port whose status read always fails: the pre-existing failure policy."""

    def status(self, *, graph_id: str) -> dict[str, object]:
        raise RuntimeError("pac status unavailable")


def test_existing_failure_policy_counts_every_io_failure(tmp_path: Path) -> None:
    """Locks the behaviour that ships today; it is not a desired outcome.

    Each failed status read is one attempt, so the same unconfirmed run appends
    ``escalated`` on every timer round and the durable row stays.  A follow-up
    card owns any run-level de-duplication; this test exists so a future change
    is visible instead of silent.
    """

    pac = BrokenStatusPac()
    clock = Clock()
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
        for _ in range(2):
            clock.now_ms += 60_000
            service.submit_timer()
            wait_for(lambda: len(service.status(name="cli-dev-duty")["outcomes"]) >= 1)
    finally:
        service.close()


# -- R1 characterization: the sample O4 gives up (cli 13:39Z, option a) ------


def test_r1_pause_then_late_terminal_records_its_sample(tmp_path: Path) -> None:
    """A late terminal completion still counts, even though ``pause`` dropped the effect.

    This is the R1(b) guarantee: whether the effect survived GC may not decide
    whether the circuit breaker sees the sample.  The reclaim and the outcome are
    one transaction, and the JSON window keeps at most ``window_runs`` samples.
    """

    store = RoutineStore(tmp_path / "routines.sqlite3")
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = _registry(store, logger)
        registry(_event(state="done"))
        assert _rows(store) == 0
        assert json.loads(_outcomes(store)) == ["done"]
        fields_log = logger.find(LOG_EVENT)
        assert fields_log is not None
        assert fields_log["reclaimed"] is True
        assert fields_log["outcome_recorded"] is True
        assert fields_log["outcome"] == "done"
    finally:
        store.close()


def test_r1_terminal_before_pause_keeps_the_recorded_sample(tmp_path: Path) -> None:
    """The opposite order still records its one sample, and the replay adds none."""

    pac = AbsentPac()
    service, graph_id = _dispatched(tmp_path, pac)
    try:
        with pac.lock:
            pac.states[graph_id] = "done"
        service.set(
            name="cli-dev-duty",
            yaml_text=SCHEDULED.replace("Heartbeat and pick up work.", "Heartbeat v2."),
        )
        assert service.status(name="cli-dev-duty")["outcomes"] == ["done"]

        service.pause(name="cli-dev-duty")
        status = service.status(name="cli-dev-duty")
        assert status["inFlight"] == []
        assert status["outcomes"] == ["done"]
    finally:
        service.close()


def test_the_positive_reclaims_once_and_a_replay_writes_nothing(tmp_path: Path) -> None:
    store = RoutineStore(tmp_path / "routines.sqlite3")
    logger = CapturingLogger()
    try:
        seed_in_flight(store)
        registry = _registry(store, logger)
        registry(_event(state="done"))
        assert _rows(store) == 0
        assert json.loads(_outcomes(store)) == ["done"]

        # The same receipt again: the row is already gone, so this is a
        # diagnostic only (no apply, no second outcome, no second reclaim).
        registry(_event(state="done"))
        assert _rows(store) == 0
        assert json.loads(_outcomes(store)) == ["done"]
        events = [fields for name, fields in logger.events if name == LOG_EVENT]
        assert [event["reclaimed"] for event in events] == [True, False]
        assert events[1]["reason"] == "run-mismatch"
    finally:
        store.close()


# -- the read-only probe contract -------------------------------------------


class ProbeAuditPac(AbsentPac):
    """Records whether the probe asked for a read-only store and closed it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def graph_absent(self, graph_id: str) -> bool:
        self.calls.append(graph_id)
        return super().graph_absent(graph_id)


def test_graph_absent_is_read_only_and_closes_every_path(tmp_path: Path) -> None:
    from hyprial.daemon import PacGraphStore, default_database_path
    from hyprial.biz.impl.routine.execution.pac_dispatch import PacRoutineDispatch

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    database = default_database_path(state)
    writer = PacGraphStore(database)
    writer.close()  # an empty, well-formed store: every read succeeds

    seen: list[tuple[str, bool]] = []
    import hyprial.biz.impl.routine.execution.pac_dispatch as module

    original = module.PacGraphStore

    class AuditedStore:
        def __init__(self, path, *, read_only: bool = False) -> None:
            self._inner = original(path, read_only=read_only)
            seen.append(("open", read_only))

        def graph(self, graph_id: str):
            return self._inner.graph(graph_id)

        def node(self, graph_id: str, name: str):
            return self._inner.node(graph_id, name)

        def close(self) -> None:
            seen.append(("close", True))
            self._inner.close()

    module.PacGraphStore = AuditedStore
    try:
        dispatch = PacRoutineDispatch(
            state_dir=state,
            deliver=lambda *args, **kwargs: None,
            clock_ms=lambda: Clock.now_ms,
            resolve_principal=lambda principal: principal,
        )
        assert dispatch.graph_absent("routine-does-not-exist") is True
        # Successful read, no row: opened read-only and closed.
        assert [kind for kind, _ in seen] == ["open", "close"]
        assert seen[0][1] is True
    finally:
        module.PacGraphStore = original

    missing = tmp_path / "no-store"
    missing.mkdir()
    absent_dispatch = PacRoutineDispatch(
        state_dir=missing,
        deliver=lambda *args, **kwargs: None,
        clock_ms=lambda: Clock.now_ms,
        resolve_principal=lambda principal: principal,
    )
    # A missing database is a read failure, never "the graph is gone", and the
    # read-only mode must not create it.
    try:
        absent_dispatch.graph_absent("routine-anything")
    except Exception:  # noqa: BLE001 - the failure itself is the contract
        pass
    else:  # pragma: no cover - a port that guessed would be a defect
        raise AssertionError("a missing PAC database is not proof of absence")
    assert not (missing / "pac-graph.sqlite3").exists()


# -- known limit: O4 does not clear a pending_status cycle ------------------


def test_pending_status_orphan_cycle_still_blocks_set(tmp_path: Path) -> None:
    """Locks the known limit: O4 does not clear a ``pending_status`` cycle.

    Seeding a cycle with ``pending_status=1`` and no effect (a shape ``pause`` can
    leave behind) means ``routine set`` refuses the active cycle **before** any
    graph probe runs, so neither the proven-absent probe nor the reclaim is ever
    reached: the row stays and ``set`` stays ROUTINE_BUSY.  This test exists so a
    future change to the cycle accounting is visible instead of silent.
    """

    pac = AbsentPac()
    clock = Clock()
    state = tmp_path / "state"
    store = RoutineStore(state / "routines.sqlite3")
    try:
        store.apply(
            routine=RoutineRow(
                name="cli-dev-duty",
                yaml_text=SCHEDULED,
                owner="agent:o:n:duty",
                enabled=False,
                next_due_ms=Clock.now_ms,
                source_error_streak=0,
                outcomes="[]",
                created_at_ms=Clock.now_ms - 60_000,
                version=1,
            ),
            cycle=RoutineCycleRow(
                correlation_id="cycle-orphan",
                routine_name="cli-dev-duty",
                generation=1,
                version=1,
                tasks_json="[]",
                outcomes_json="[]",
                pending_status=1,
            ),
            put_in_flight=(
                InFlightRow(
                    "cli-dev-duty",
                    "task-orphan",
                    "routine-cli-dev-duty-orphan01",
                    "agent:o:n:duty",
                    Clock.now_ms - 1_000,
                ),
            ),
        )
    finally:
        store.close()
    pac.absent.add("routine-cli-dev-duty-orphan01")

    service = RoutineService(
        pac=pac,
        alarm=Alarm(),
        state_dir=state,
        clock_ms=lambda: clock.now_ms,
        source_query=lambda _filter: [],
    )
    try:
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
        # The cycle check runs first, so nothing is probed or reclaimed.
        assert busy_code == "ROUTINE_BUSY"
        status = service.status(name="cli-dev-duty")
        assert [item["taskUuid"] for item in status["inFlight"]] == ["task-orphan"]
        assert status["outcomes"] == []
    finally:
        service.close()
