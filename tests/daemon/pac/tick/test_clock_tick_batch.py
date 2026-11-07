"""P2 (batch): one authority round trip for every clock job in a tick.

Measured on branch=authority, fixture=29 open+activated graphs, in-process:
  authority.clock_tick(gid) single      1.082 ms
  fixed round trip (missing graph)      0.880 ms
  PacReactor(store).tick_clocks(gid)    0.008 ms  (no pending clock work)
  => per-graph handler work              0.202 ms
so the tick pays 29 round trips.

Budget, derived from the owner's cadence change rather than from the old
per-call number: batching makes one clock call per tick, i.e. 0.91 calls per
second, and the owner's cap is 0.10 core, so

  0.91 * (0.880 + 29 * handler_per_graph) <= 100 ms
  => handler_per_graph <= 3.4 ms

The earlier 3.8 ms/0.100 ms pair assumed the cadence stayed at 26.4 calls per
second; it does not once the tick is batched. If the handler cannot reach
3.4 ms per graph the route misses the cap and is reported as such.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time

import pytest

from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_node, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path

USER = "user:allen"
BUDGET_MS = 3.8
ROUND_TRIP_MS = 0.880
HANDLER_BUDGET_MS = 3.4
CALLS_PER_SECOND = 0.91


def _graphs(tmp_path: Path, count: int) -> tuple[Path, list[str]]:
    state = tmp_path / "state"
    state.mkdir()
    database = default_database_path(state)
    store = PacGraphStore(database)
    try:
        graph_ids = []
        for index in range(count):
            graph_id = create_graph(store, name=f"g{index}", created_by=USER)["graphId"]
            activate_graph(store, graph_id, actor=USER)
            graph_ids.append(graph_id)
        return database, graph_ids
    finally:
        store.close()


def test_one_clock_tick_call_covers_every_graph_within_the_budget(tmp_path: Path) -> None:
    database, graph_ids = _graphs(tmp_path, 29)
    authority = PacGraphAuthority(database, NullSender())
    calls: list[object] = []
    real_call = authority._call

    def counting_call(command: object):
        calls.append(command)
        return real_call(command)

    authority._call = counting_call  # type: ignore[method-assign]
    try:
        started = time.perf_counter()
        result = authority.clock_tick_many(graph_ids)
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        authority.close(3.0)

    assert len(calls) == 1, f"expected one round trip, saw {len(calls)}"
    assert result["graphs"] == len(graph_ids), result
    handler_ms = (elapsed_ms - ROUND_TRIP_MS) / len(graph_ids)
    assert handler_ms <= HANDLER_BUDGET_MS, (
        f"handler_per_graph {handler_ms:.3f} ms > {HANDLER_BUDGET_MS} ms "
        f"(batch {elapsed_ms:.3f} ms); at {CALLS_PER_SECOND} calls/s that is "
        f"{CALLS_PER_SECOND * elapsed_ms:.1f} ms/s, over the 100 ms/s cap"
    )


def test_a_failing_command_rolls_back_only_itself(tmp_path: Path) -> None:
    """Per-command granularity survives the shared connection (SAVEPOINT).

    Batching shares one connection and one transaction, so a failing command
    must not discard its predecessors: the first k-1 entries stay applied, the
    k-th is rolled back, and nothing after k runs (the owner's rule for P6 and
    for this call: never drop the whole batch).
    """

    database, graph_ids = _graphs(tmp_path, 5)
    ordered = [graph_ids[0], graph_ids[1], "wf-does-not-exist", graph_ids[3], graph_ids[4]]
    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.clock_tick_many(ordered, stop_on_error=True)
    finally:
        authority.close(3.0)

    results = result["results"]
    assert len(results) == 3, f"execution must stop at the failing entry: {results}"
    assert results[0]["ok"] is True and results[1]["ok"] is True, results[:2]
    assert results[2]["ok"] is False, results[2]
    assert results[2]["code"] == "PAC_GRAPH_NOT_FOUND", results[2]


def test_the_two_savings_are_independently_attributable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-trip batching and connection reuse must be separable.

    The owner needs to know which of the two wins matters in production, and
    the fixture's ratio need not hold there. `shared_connection=False` keeps
    the single round trip (saving 1) while still opening one connection per
    graph (not saving 2), so the two effects can be measured apart.
    """

    database, graph_ids = _graphs(tmp_path, 5)
    authority = PacGraphAuthority(database, NullSender())
    opens: list[str] = []
    real_init = PacGraphStore.__init__

    def counting_init(self, *args, **kwargs):
        # Count only the handler's own stores.  The authority's delivery loop
        # opens a read-only store on its own cadence in a background thread,
        # and folding those in would make this measurement racy rather than
        # stricter.
        if kwargs.get("read_only") is not True:
            opens.append(str(args[0]) if args else "")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(PacGraphStore, "__init__", counting_init)
    try:
        opens.clear()
        shared = authority.clock_tick_many(graph_ids, shared_connection=True)
        shared_opens = len(opens)

        opens.clear()
        separate = authority.clock_tick_many(graph_ids, shared_connection=False)
        separate_opens = len(opens)
    finally:
        authority.close(3.0)

    assert shared["graphs"] == separate["graphs"] == len(graph_ids)
    assert shared_opens == 1, f"shared connection must open one store, saw {shared_opens}"
    assert separate_opens == len(graph_ids), (
        f"without connection reuse every graph opens its own store, saw {separate_opens}"
    )


def test_the_production_default_isolates_a_bad_graph_and_continues(tmp_path: Path) -> None:
    """Production today is per-graph: one bad graph must not stop the others.

    `stop_on_error` defaults to the production behaviour (False), so a batch of
    [good, bad, good] ticks both good graphs and records the bad one, instead of
    turning one broken graph into a batch-wide regression.
    """

    database, graph_ids = _graphs(tmp_path, 4)
    ordered = [graph_ids[0], "wf-does-not-exist", graph_ids[2], graph_ids[3]]
    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.clock_tick_many(ordered)
    finally:
        authority.close(3.0)

    results = result["results"]
    assert result["graphs"] == len(ordered), result
    assert len(results) == len(ordered), f"every graph must be attempted: {results}"
    assert results[0]["ok"] is True and results[2]["ok"] is True and results[3]["ok"] is True
    assert results[1]["ok"] is False and results[1]["code"] == "PAC_GRAPH_NOT_FOUND", results[1]
    # One isolated failure is not systemic: nothing was skipped, nothing aborted.
    assert result["unattempted"] == 0, result
    assert result["aborted_after"] is None and result["abort_reason"] is None, result


def test_any_failure_is_isolated_to_its_own_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production isolation is per graph, for EVERY failure class.

    The old per-graph loop caught `Exception`, so a malformed graph -- a
    `JSONDecodeError` from a corrupt launch document, a `TypeError` from a
    shape the reactor did not expect -- rolled back only itself. Catching only
    the database errors would let one such graph abort the batch and leave
    every healthy graph unticked for that tick, which is the regression this
    test pins.
    """

    database, graph_ids = _graphs(tmp_path, 4)
    broken = graph_ids[1]
    real_tick = PacReactor.tick_clocks
    ticked: list[str] = []

    def flaky_tick(self: PacReactor, graph_id: str):
        if graph_id == broken:
            raise TypeError("injected non-database failure")
        ticked.append(graph_id)
        return real_tick(self, graph_id)

    monkeypatch.setattr(PacReactor, "tick_clocks", flaky_tick)
    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.clock_tick_many(graph_ids)
    finally:
        authority.close(3.0)

    results = result["results"]
    assert len(results) == len(graph_ids), (
        "a non-database failure must not stop the batch: " f"{results}"
    )
    entry = next(item for item in results if item["graphId"] == broken)
    assert entry["ok"] is False, entry
    assert entry["code"] == "PAC_CLOCK_TICK_FAILED", entry
    assert entry["detail"] == "TypeError", entry
    assert ticked == [graph_ids[0], graph_ids[2], graph_ids[3]], ticked
    assert result["aborted_after"] is None and result["unattempted"] == 0, result


def test_committed_work_survives_a_later_graph_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each graph commits on its own: a later failure must not undo earlier work.

    The isolation boundary is the graph, so a batch that dies on graph 3 must
    leave graph 1's committed notifications in place. Mutating the per-graph
    rollback into a commit (or rolling the whole batch back) flips this test:
    the earlier graphs' overdue notifications disappear.
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = default_database_path(state_dir)
    store = PacGraphStore(database)
    try:
        healthy = [
            _overdue_clock_graph(store, f"healthy-{index}") for index in range(2)
        ]
        broken = _overdue_clock_graph(store, "broken")
        assert _overdue_notifications(store, healthy) == [0, 0]
    finally:
        store.close()

    # The failure lands INSIDE the graph's own transaction, after its rows are
    # written and before COMMIT: that is the only shape where turning the
    # rollback into a commit becomes observable (a failure before the write
    # would leave nothing behind either way).
    real_insert = PacReactor._insert_notifications

    def insert_then_fail(self: PacReactor, planned, now, **kwargs):
        rows = real_insert(self, planned, now, **kwargs)
        if kwargs.get("graph_id") == broken:
            raise TypeError("injected after the rows were written, before COMMIT")
        return rows

    monkeypatch.setattr(PacReactor, "_insert_notifications", insert_then_fail)
    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.clock_tick_many([*healthy, broken])
    finally:
        authority.close(3.0)

    assert [entry["ok"] for entry in result["results"]] == [True, True, False]

    store = PacGraphStore(database)
    try:
        counts = _overdue_notifications(store, [*healthy, broken])
    finally:
        store.close()
    assert counts[:2] != [0, 0], (
        "the healthy graphs' notifications must stay committed: " f"{counts}"
    )
    assert counts[2] == 0, "the failed graph must roll back its own work: " f"{counts}"


def _overdue_clock_graph(store: PacGraphStore, name: str) -> str:
    """One activated graph whose clock node is already overdue."""

    graph_id = create_graph(store, name=name, created_by=USER)["graphId"]
    version = add_node(
        store,
        Path(store.path).parent,
        graph_id=graph_id,
        expect_version=1,
        node_id="tick",
        owner=USER,
        brief_ref=f"brief#{name}",
        kind="clock",
        deadline_ms=1,
    )["version"]
    assert version == 2
    activate_graph(store, graph_id, actor=USER)
    return graph_id


def _overdue_notifications(store: PacGraphStore, graph_ids: list[str]) -> list[int]:
    """Count the overdue notifications each graph has written."""

    return [
        int(
            store._db.execute(
                "SELECT count(*) FROM notifications WHERE kind='overdue' "
                "AND event_id LIKE ?",
                (f"overdue:{graph_id}:%",),
            ).fetchone()[0]
        )
        for graph_id in graph_ids
    ]


def test_repeated_per_graph_failures_do_not_abort_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run of bad graphs is not a systemic stop: the good ones still tick.

    Round-3 review probe: [good, bad, bad, bad, good] produced notifications
    [1, 0, 0, 0, 0] because every ordinary exception fed the same
    consecutive-failure counter that the systemic early stop uses. Per-graph
    isolation means the LAST healthy graph is served in that same tick --
    baseline [1, 0, 0, 0, 1].
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = default_database_path(state_dir)
    store = PacGraphStore(database)
    try:
        healthy_first = _overdue_clock_graph(store, "healthy-first")
        broken = [_overdue_clock_graph(store, f"broken-{index}") for index in range(3)]
        healthy_last = _overdue_clock_graph(store, "healthy-last")
    finally:
        store.close()

    real_tick = PacReactor.tick_clocks

    def failing_tick(self: PacReactor, graph_id: str):
        if graph_id in broken:
            raise TypeError(f"injected per-graph failure for {graph_id}")
        return real_tick(self, graph_id)

    monkeypatch.setattr(PacReactor, "tick_clocks", failing_tick)
    ordered = [healthy_first, *broken, healthy_last]
    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.clock_tick_many(ordered)
    finally:
        authority.close(3.0)

    assert len(result["results"]) == len(ordered), result["results"]
    assert result["aborted_after"] is None, result
    assert result["unattempted"] == 0, result

    store = PacGraphStore(database)
    try:
        counts = _overdue_notifications(store, ordered)
    finally:
        store.close()
    assert counts == [1, 0, 0, 0, 1], counts


def test_constraint_failures_are_per_graph_not_systemic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected row is that graph's problem, even though it is a sqlite error.

    Round-4 review: treating every sqlite3.Error as systemic made three graphs
    whose journal trigger aborted the INSERT (IntegrityError) starve the trailing
    healthy graph -- notifications [1, 0, 0, 0, 0]. Only a store that cannot be
    used at all (OperationalError: locked, I/O, corrupt, unable to open) dooms
    the batch; a constraint rejection does not.
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = default_database_path(state_dir)
    store = PacGraphStore(database)
    try:
        healthy_first = _overdue_clock_graph(store, "healthy-first")
        rejected = [_overdue_clock_graph(store, f"rejected-{index}") for index in range(3)]
        healthy_last = _overdue_clock_graph(store, "healthy-last")
    finally:
        store.close()

    real_tick = PacReactor.tick_clocks

    def failing_tick(self: PacReactor, graph_id: str):
        if graph_id in rejected:
            raise sqlite3.IntegrityError("journal is append-only")
        return real_tick(self, graph_id)

    monkeypatch.setattr(PacReactor, "tick_clocks", failing_tick)
    ordered = [healthy_first, *rejected, healthy_last]
    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.clock_tick_many(ordered)
    finally:
        authority.close(3.0)

    assert result["aborted_after"] is None, result
    assert result["unattempted"] == 0, result
    assert [entry["ok"] for entry in result["results"]] == [True, False, False, False, True]

    store = PacGraphStore(database)
    try:
        counts = _overdue_notifications(store, ordered)
    finally:
        store.close()
    assert counts == [1, 0, 0, 0, 1], counts


def test_a_systemic_failure_stops_the_batch_and_reports_the_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A storage layer that keeps failing dooms the batch; a bad graph does not.

    Trying every remaining entry multiplies cost, log noise and rollbacks by the
    batch size and buries the single cause, so the batch stops after K=3
    consecutive storage failures. The skip boundary is the storage class, not
    any failure: three broken graphs in a row must NOT stop the batch (see
    test_repeated_per_graph_failures_do_not_abort_the_batch).
    """

    database, graph_ids = _graphs(tmp_path, 4)
    attempted: list[str] = []
    real_tick = PacReactor.tick_clocks

    def failing_tick(self: PacReactor, graph_id: str):
        attempted.append(graph_id)
        raise sqlite3.OperationalError("injected: database is gone")
        return real_tick(self, graph_id)  # pragma: no cover - unreachable

    monkeypatch.setattr(PacReactor, "tick_clocks", failing_tick)
    authority = PacGraphAuthority(database, NullSender())
    try:
        result = authority.clock_tick_many(graph_ids)
    finally:
        authority.close(3.0)

    assert result["graphs"] == len(graph_ids), result
    assert len(result["results"]) == 3, result["results"]
    assert all(entry["code"] == "PAC_CLOCK_TICK_FAILED" for entry in result["results"])
    assert all(entry["detail"] == "OperationalError" for entry in result["results"])
    assert result["aborted_after"] == 3, result
    assert result["abort_reason"] == "PAC_CLOCK_TICK_FAILED", result
    assert result["unattempted"] == 1, result
    assert attempted == graph_ids[:3], attempted
