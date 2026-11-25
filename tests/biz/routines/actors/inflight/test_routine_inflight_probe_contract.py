"""The PAC probe contract: what a failed read is allowed to conclude.

Split out of ``test_routine_inflight_reclaim`` to stay inside the 1000-line
module budget; these tests still share its fakes and helpers.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.biz.routines.actors.inflight.test_routine_inflight_reclaim import (
    AbsentPac,
    SCHEDULED,
    _dispatched,
)
from tests.biz.routines.actors.inflight.test_routine_paused_completion_orphan import (
    Clock,
    RoutineStore,
    wait_for,
)

# -- §4 #6: the read-only probe contract on every path ----------------------


def _store_with_graph(state: Path) -> str:
    """Create one real graph row and return its id (the probe reads this row)."""

    from hyprial.daemon import PacGraphStore, create_graph

    store = PacGraphStore(default_graph_db(state))
    try:
        created = create_graph(
            store,
            name="routine-probe",
            created_by="agent:o:n:duty",
        )
    finally:
        store.close()
    return str(created["graphId"])


def default_graph_db(state: Path):
    from hyprial.daemon import default_database_path

    return default_database_path(state)


def _dispatch(state: Path):
    from hyprial.biz.impl.routine.execution.pac_dispatch import PacRoutineDispatch

    return PacRoutineDispatch(
        state_dir=state,
        deliver=lambda *args, **kwargs: None,
        clock_ms=lambda: Clock.now_ms,
        resolve_principal=lambda principal: principal,
    )


def test_probe_closes_on_success_with_and_without_a_row(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    present = _store_with_graph(state)
    closed: list[str] = []
    import hyprial.biz.impl.routine.execution.pac_dispatch as module

    original = module.PacGraphStore

    class AuditedStore:
        def __init__(self, path, *, read_only: bool = False) -> None:
            assert read_only is True, "the probe must never open a writable store"
            self._inner = original(path, read_only=read_only)

        def graph(self, graph_id: str):
            return self._inner.graph(graph_id)

        def close(self) -> None:
            closed.append("close")
            self._inner.close()

    module.PacGraphStore = AuditedStore
    try:
        dispatch = _dispatch(state)
        assert dispatch.graph_absent(present) is False
        assert dispatch.graph_absent("routine-missing-000001") is True
    finally:
        module.PacGraphStore = original
    assert closed == ["close", "close"]


def test_probe_closes_when_the_read_raises(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    closed: list[str] = []
    import hyprial.biz.impl.routine.execution.pac_dispatch as module

    original = module.PacGraphStore

    class RaisingStore:
        def __init__(self, path, *, read_only: bool = False) -> None:
            assert read_only is True

        def graph(self, graph_id: str):
            raise RuntimeError("database disk image is malformed")

        def close(self) -> None:
            closed.append("close")

    module.PacGraphStore = RaisingStore
    try:
        dispatch = _dispatch(state)
        try:
            dispatch.graph_absent("routine-anything")
        except RuntimeError:
            pass
        else:  # pragma: no cover - a corrupt read is never absence
            raise AssertionError("a failing read must not be reported as absence")
    finally:
        module.PacGraphStore = original
    assert closed == ["close"]


def test_probe_against_a_corrupt_database_is_not_absence(tmp_path: Path) -> None:
    """A corrupt store is a read failure, never proof that the graph is gone."""

    import sqlite3

    state = tmp_path / "state"
    state.mkdir()
    database = default_graph_db(state)
    database.write_bytes(b"this is not a sqlite database")
    before = database.stat().st_size

    dispatch = _dispatch(state)
    with pytest.raises(sqlite3.DatabaseError):
        dispatch.graph_absent("routine-anything")
    assert database.stat().st_size == before


def test_probe_against_a_locked_database_is_not_absence(tmp_path: Path) -> None:
    """A held write lock is a read failure, never proof that the graph is gone.

    The lock is built deterministically by a second connection: the database is
    switched to a rollback journal (WAL lets readers proceed, a rollback journal
    does not) and then held under ``BEGIN EXCLUSIVE``, so the read-only probe must
    block and then raise instead of returning ``None``.
    """

    import sqlite3

    state = tmp_path / "state"
    state.mkdir()
    database = default_graph_db(state)
    locked = _store_with_graph(state)

    before = database.stat().st_size
    blocker = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        blocker.execute("BEGIN EXCLUSIVE")
        dispatch = _dispatch(state)
        started = time.monotonic()
        # The default busy timeout (~5 s per attempt) makes this the slowest
        # assertion in the suite; the production timeout is not changed.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            dispatch.graph_absent(locked)
        elapsed = time.monotonic() - started
        # A read-only probe must not have removed, truncated or "repaired" the
        # locked database while concluding nothing.
        assert database.stat().st_size == before
        assert elapsed < 60.0, f"the lock probe took {elapsed:.1f}s"
        print(f"\n[lock probe elapsed] {elapsed:.2f}s")
    finally:
        blocker.rollback()
        blocker.close()


class StoreBackedAbsentPac(AbsentPac):
    """An absent-probe fake that really reads the PAC store.

    The service-level tests below must fail *in the store*, so the fake cannot
    answer from a set: it delegates to the production ``PacRoutineDispatch`` and
    therefore opens the same read-only database the real probe opens.
    """

    def __init__(self, state: Path) -> None:
        super().__init__()
        self._dispatch = _dispatch(state)
        self.store_reads = 0

    def graph_absent(self, graph_id: str) -> bool:
        self.probe_calls.append(graph_id)
        self.store_reads += 1
        return self._dispatch.graph_absent(graph_id)


# -- §4 #7: an absent apply that fails leaves both sides untouched ----------


def test_a_failing_absent_apply_rolls_back_and_stays_quiet(tmp_path: Path) -> None:
    pac = AbsentPac()
    service, graph_id = _dispatched(tmp_path, pac)
    try:
        with pac.lock:
            pac.states[graph_id] = "done"
        pac.absent.add(graph_id)
        service.pause(name="cli-dev-duty")

        original_apply = RoutineStore.apply
        calls: list[str] = []

        def failing_apply(self, *args, **kwargs):
            calls.append("apply")
            raise RuntimeError("simulated commit failure")

        RoutineStore.apply = failing_apply
        try:
            try:
                service.set(
                    name="cli-dev-duty",
                    yaml_text=SCHEDULED.replace(
                        "Heartbeat and pick up work.", "Heartbeat v2."
                    ),
                )
            except Exception:  # noqa: BLE001 - the failure itself is the subject
                pass
        finally:
            RoutineStore.apply = original_apply

        assert calls, "the rollback case must actually reach the store"
        status = service.status(name="cli-dev-duty")
        # Neither the row nor an outcome moved, and no terminal sample was
        # invented by the failed transaction.
        assert [item["taskUuid"] for item in status["inFlight"]] == [
            next(iter(service.status(name="cli-dev-duty")["inFlight"]))["taskUuid"]
        ]
        assert status["outcomes"] == []
    finally:
        service.close()


# -- D.5: a slow probe cannot interleave with pause/resume ------------------


class SlowPac(AbsentPac):
    """A PAC whose ``status`` read the test releases explicitly; no sleeping."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocking = False

    def status(self, *, graph_id: str) -> dict[str, object]:
        if self.blocking:
            self.entered.set()
            assert self.release.wait(5.0), "the slow status read was never released"
        return super().status(graph_id=graph_id)


@pytest.mark.parametrize("state", ["done", "escalated"])
def test_a_slow_probe_orders_set_before_a_queued_pause(
    tmp_path: Path, state: str
) -> None:
    pac = SlowPac()
    service, graph_id = _dispatched(tmp_path, pac)
    pause_done = threading.Event()
    outcome: dict[str, object] = {}
    try:
        with pac.lock:
            pac.states[graph_id] = state
        wait_for(lambda: service._projection.cycle_for_routine("cli-dev-duty") is None)
        pac.blocking = True

        def run_set() -> None:
            try:
                outcome["result"] = service.set(
                    name="cli-dev-duty",
                    yaml_text=SCHEDULED.replace(
                        "Heartbeat and pick up work.", "Heartbeat v2."
                    ),
                )
            except Exception as error:  # noqa: BLE001
                outcome["error"] = error

        setter = threading.Thread(target=run_set, name="slow-set")
        setter.start()
        assert pac.entered.wait(5.0), "set never reached the probe"

        def run_pause() -> None:
            service.pause(name="cli-dev-duty")
            pause_done.set()

        pauser = threading.Thread(target=run_pause, name="queued-pause")
        pauser.start()
        # The actor is inside the (slow) probe, so the pause cannot have run yet.
        assert pause_done.wait(0.2) is False

        pac.release.set()
        setter.join(5.0)
        pauser.join(5.0)

        assert pause_done.is_set()
        status = service.status(name="cli-dev-duty")
        assert status["outcomes"] == [state]
        assert status["inFlight"] == []
    finally:
        pac.release.set()
        service.close()


# -- §4 #6 (service view): a failed probe keeps the routine busy -------------


@pytest.mark.parametrize("failure", ["sql", "corrupt", "lock"])
def test_a_failed_probe_keeps_the_row_and_stays_busy(
    tmp_path: Path, failure: str
) -> None:
    """No store failure may be read as "settled" or as "absent".

    ``sql``/``corrupt``/``lock`` are the store-level failures; the port-level ones
    (missing capability, a raising ``status``) are covered separately by
    ``test_a_port_without_the_capability_keeps_the_row`` and
    ``test_a_probe_failure_is_not_absence``.  In every case the row survives and
    ``set`` refuses with ROUTINE_BUSY instead of dropping work it cannot account
    for.
    """

    import sqlite3

    state = tmp_path / "state"
    state.mkdir()
    pac = StoreBackedAbsentPac(state)
    _store_with_graph(state)
    database = default_graph_db(state)
    blocker = None
    if failure == "corrupt":
        database.write_bytes(b"not a database")
    elif failure == "lock":
        blocker = sqlite3.connect(database, timeout=0, isolation_level=None)
        assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        blocker.execute("BEGIN EXCLUSIVE")

    service, _graph_id = _dispatched(tmp_path, pac)
    import hyprial.biz.impl.routine.execution.pac_dispatch as module

    original_graph = module.PacGraphStore.graph

    sql_reads: list[str] = []

    def broken_graph(self, graph_id: str):
        sql_reads.append(graph_id)
        raise sqlite3.OperationalError("no such table: graphs")

    try:
        if failure == "sql":
            module.PacGraphStore.graph = broken_graph
        service.pause(name="cli-dev-duty")
        with pytest.raises(Exception) as caught:  # noqa: BLE001 - code asserted
            service.set(
                name="cli-dev-duty",
                yaml_text=SCHEDULED.replace(
                    "Heartbeat and pick up work.", "Heartbeat v2."
                ),
            )
        assert getattr(caught.value, "code", None) == "ROUTINE_BUSY"
        status = service.status(name="cli-dev-duty")
        assert len(status["inFlight"]) == 1
        assert status["outcomes"] == []
        # Prove the fake really reached the store instead of answering from a
        # set: the delegated dispatch ran, and for `sql` the patched read ran.
        assert pac.store_reads >= 1, "the fake never opened the PAC store"
        if failure == "sql":
            assert sql_reads, "the patched store read was never reached"
    finally:
        module.PacGraphStore.graph = original_graph
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        service.close()
