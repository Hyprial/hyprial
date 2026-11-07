"""``notifications_by_synthetic_event`` filters by graph in SQL, unchanged.

Production 2026-10-06: the workflow notifier called it once per tick graph,
and the unfiltered query json-parsed every historical synthetic row each
time. The SQL filter must return exactly what the old Python filter did.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.pac.storage.store import (
    PacGraphStore,
    _notification_row,
    default_database_path,
)


def _reference(store: PacGraphStore, graph_id: str) -> list[object]:
    """The pre-2026-10-06 implementation, verbatim."""

    return [
        _notification_row(row)
        for row in store._db.execute(
            "SELECT n.* FROM notifications n "
            "WHERE n.event_id NOT IN (SELECT event_id FROM flag_events) "
            "ORDER BY n.event_id, n.edge"
        )
        if (json.loads(row["plan_json"])["graphId"] == graph_id
            if row["plan_json"] else row["event_id"].startswith(f"overdue:{graph_id}:"))
    ]


def test_graph_filter_in_sql_matches_the_python_filter(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state_dir=state))
    try:
        _check(store)
    finally:
        store.close()


def _check(store: PacGraphStore) -> None:
    rows = [
        ("overdue:g1:v1:C:1", None),
        ("overdue:g1:v2:C:2", None),
        ("overdue:g10:v1:C:1", None),  # prefix trap for g1
        ("overdue:g2:v1:C:1", None),
        ("workflow-request:a", json.dumps({"graphId": "g1"})),
        ("workflow-request:b", json.dumps({"graphId": "g10"})),
        ("workflow-request:c", json.dumps({"graphId": "g2", "x": 1})),
        ("overdue:g1:v3:C:3", json.dumps({"graphId": "g2"})),  # plan wins over id
    ]
    with store._db:
        for index, (event_id, plan) in enumerate(rows):
            for edge in ("e1", "e2"):
                store._db.execute(
                    "INSERT INTO notifications(event_id, edge, kind, recipient, text, "
                    "sender, at, plan_json) VALUES(?, ?, 'overdue', 'r', 't', 's', ?, ?)",
                    (event_id, edge, index, plan),
                )

    for graph_id in ("g1", "g10", "g2", "g3"):
        assert store.notifications_by_synthetic_event(graph_id) == _reference(
            store, graph_id
        ), graph_id
    assert [row.event_id for row in store.notifications_by_synthetic_event("g1")] == [
        "overdue:g1:v1:C:1", "overdue:g1:v1:C:1",
        "overdue:g1:v2:C:2", "overdue:g1:v2:C:2",
        "workflow-request:a", "workflow-request:a",
    ]


def test_graph_filter_is_answered_from_indexes_not_a_table_scan(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state_dir=state))
    plans: list[str] = []
    real_execute = store._db.execute

    class _Recording:
        def execute(self, sql: str, *args: object) -> object:
            if sql.startswith("SELECT n.* FROM notifications n"):
                plans.extend(
                    str(row[3]) for row in real_execute("EXPLAIN QUERY PLAN " + sql, *args)
                )
            return real_execute(sql, *args)

    try:
        monkeypatch.setattr(store, "_db", _Recording())
        store.notifications_by_synthetic_event("g1")
    finally:
        monkeypatch.undo()
        store.close()
    assert plans, "the synthetic-event query did not run"
    assert not any(step.startswith("SCAN n") for step in plans), plans
