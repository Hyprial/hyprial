"""P1: the tick's clock-job set must be computed by SQL, not filtered in Python.

`_scan_and_admit` currently selects every graph and filters
`activated_at IS NOT NULL AND closed_at IS NULL` in Python (daemon.py:536-544).
Closed-graph cleanup has its own query (:625) and never arrives through the clock
jobs, so the four cells below are the contract for both shapes -- the SQL
push-down must be an equivalent rewrite, not a behaviour change:

| graph state                     | clock job |
| ------------------------------- | --------- |
| open, activated                 | in        |
| open, never activated           | out       |
| closed, cycle settled           | out       |
| closed, cleanup pending         | out       |

The RED also counts the rows the tick query fetches: today it fetches every
historical graph, after the fix only the open+activated ones.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.pac.graphs.edits import activate_graph, close_graph, create_graph
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path

USER = "user:allen"


def _store(tmp_path: Path) -> PacGraphStore:
    state = tmp_path / "state"
    state.mkdir()
    return PacGraphStore(default_database_path(state))


def test_tick_clock_jobs_are_open_activated_graphs_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    open_activated = create_graph(store, name="open-activated", created_by=USER)["graphId"]
    activate_graph(store, open_activated, actor=USER)
    open_never = create_graph(store, name="open-never-activated", created_by=USER)["graphId"]
    closed_settled = create_graph(store, name="closed-settled", created_by=USER)["graphId"]
    activate_graph(store, closed_settled, actor=USER)
    close_graph(store, closed_settled, actor=USER)
    closed_cleanup = create_graph(store, name="closed-cleanup", created_by=USER)["graphId"]
    activate_graph(store, closed_cleanup, actor=USER)
    close_graph(store, closed_cleanup, actor=USER)

    try:
        cells = {
            "open+activated": (open_activated, "in"),
            "open+never-activated": (open_never, "out"),
            "closed+settled": (closed_settled, "out"),
            "closed+cleanup": (closed_cleanup, "out"),
        }
        rows, jobs = store.tick_clock_jobs()
        fetched = len(rows)
        # The legacy set is derived INDEPENDENTLY: a raw query over the graphs
        # table plus the literal old predicate, never the new method's output.
        legacy_rows = store._db.execute(
            "SELECT graph_id, activated_at, closed_at FROM graphs ORDER BY graph_id"
        ).fetchall()
        legacy = {
            str(row["graph_id"])
            for row in legacy_rows
            if row["activated_at"] is not None and row["closed_at"] is None
        }
        for cell, (graph_id, expected) in cells.items():
            assert (graph_id in jobs) is (expected == "in"), (
                f"cell {cell}: expected {expected}, jobs={sorted(jobs)}"
            )
        assert len(jobs) == sum(1 for _, e in cells.values() if e == "in"), sorted(jobs)
        assert set(jobs) == legacy, "the SQL push-down must equal the Python filter"
        assert fetched == 1, (
            "the tick query must not fetch closed or never-activated graphs; "
            f"fetched {fetched} of {len(cells)}"
        )
    finally:
        store.close()


def test_reopening_a_closed_graph_adds_it_back_to_the_clock_jobs(tmp_path: Path) -> None:
    """Negative control: the predicate must react to closed_at, not to a constant."""

    store = _store(tmp_path)
    reopened = create_graph(store, name="reopened", created_by=USER)["graphId"]
    activate_graph(store, reopened, actor=USER)
    close_graph(store, reopened, actor=USER)
    try:
        _, before = store.tick_clock_jobs()
        assert reopened not in before, before

        with store.write() as db:
            db.execute("UPDATE graphs SET closed_at=NULL WHERE graph_id=?", (reopened,))

        _, after = store.tick_clock_jobs()
        assert reopened in after, after
    finally:
        store.close()
