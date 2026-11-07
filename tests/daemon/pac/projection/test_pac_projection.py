"""PAC v2 P3: the read-only projection over flag_events (spec ⑤).

Acceptances: the projection restates the current state (whose turn, which
round, whether anything was withdrawn) from the full event stream; the
consumer is strictly read-only (mode=ro, no write surface, file unchanged);
an incremental fold equals the full fold.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.graphs.projection import Projection, ProjectionState, fold_event, restate
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from tests.support.pac.builders import build_allen_graph, principal_uri


@pytest.fixture()
def allen_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The Allen graph plus the full slice-1 event script (happy path with
    one withdraw and one pm-不通过 back edge), as the reactor recorded it."""

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    store, graph_id = build_allen_graph(state)
    sender = NullSender()
    reactor = PacReactor(store, sender=sender)

    script = [
        ("feat-collector", "feat-bot", "set"),
        ("feat-collector", "feat-bot", "reset"),  # ③ withdraw leg
        ("feat-collector", "feat-bot", "set"),
        ("pm-helper", "pm-helper", "set"),
        ("pm-confirm-1", "pm", "set"),
        ("dev-help", "dev-help", "set"),
        ("dev-confirm", "dev", "set"),
        ("实施", "dev", "set"),
        ("demo", "dev", "set"),  # owner pm's 2nd confirmation node, by demo
        ("pm-不通过", "pm", "set"),  # ④ back edge fires pm-helper round 2
        ("pm-helper", "pm-helper", "reset"),  # redo begins
        ("pm-helper", "pm-helper", "set"),
        ("pm-confirm-1", "pm", "reset"),  # design re-confirm: cascade of
        ("pm-confirm-1", "pm", "set"),  # withdraws + re-turns flows down
        ("dev-help", "dev-help", "reset"),
        ("dev-help", "dev-help", "set"),
        ("dev-confirm", "dev", "reset"),
        ("dev-confirm", "dev", "set"),
        ("实施", "dev", "reset"),
        ("实施", "dev", "set"),
        ("demo", "dev", "reset"),
        ("demo", "dev", "set"),
        ("pm-confirm-2", "pm", "set"),  # demo confirmed; deploy leg opens
        ("deploy-help", "deploy-help", "set"),
        ("deploy-confirm", "deploy", "set"),
        ("上线", "deploy", "set"),
    ]
    for node_id, actor, action in script:
        if action == "set":
            reactor.set_flag(graph_id, node_id, actor=principal_uri(actor))
        else:
            reactor.reset_flag(graph_id, node_id, actor=principal_uri(actor))

    projection = Projection(store.path)
    yield store, graph_id, projection, sender
    store.close()


def test_projection_restates_current_turn_round_and_withdrawals(allen_flow) -> None:
    store, graph_id, projection, _sender = allen_flow
    document = projection.snapshot(graph_id)

    # every flag the script touched is set at the end; dev-不通过 is a
    # declared loop button this script never pressed
    unset = {node["node"] for node in document["state"]["nodes"] if not node["flag"]}
    assert unset == {"dev-不通过"}
    assert document["state"]["currentTurns"] == []
    # rounds counted out of flag_events: the redo cascade re-ran the chain
    rounds = {node["node"]: node["round"] for node in document["state"]["nodes"]}
    assert rounds["pm-helper"] == 2
    assert rounds["pm-confirm-1"] == 2
    assert rounds["dev-help"] == 2
    assert rounds["demo"] == 2
    assert rounds["pm-confirm-2"] == 1  # confirmed once, after the redo
    assert rounds["deploy-help"] == 1  # turned exactly once: the early-turn deviation is gone
    assert rounds["上线"] == 1
    # withdrawals: the withdraw cascade of the redo (each reset retracts the
    # turn its out-edge had delivered)
    assert {item["node"] for item in document["withdraws"]} == {
        "pm-helper",
        "pm-confirm-1",
        "dev-help",
        "dev-confirm",
        "实施",
        "demo",
        "pm-confirm-2",
    }
    # the turn log: pm-confirm-1 by pm-helper, pm-confirm-2 by demo,
    # pm-helper's round-2 ask by the back edge
    pm1_turns = [item for item in document["turns"] if item["node"] == "pm-confirm-1"]
    pm2_turns = [item for item in document["turns"] if item["node"] == "pm-confirm-2"]
    assert [item["round"] for item in pm1_turns] == [1, 2]
    assert [item["round"] for item in pm2_turns] == [1, 1]
    assert any(
        item["node"] == "pm-helper" and item["round"] == 2 for item in document["turns"]
    )
    deploy_help_turns = [item for item in document["turns"] if item["node"] == "deploy-help"]
    assert [item["round"] for item in deploy_help_turns] == [1]


def test_projection_is_strictly_read_only(allen_flow, tmp_path: Path) -> None:
    store, graph_id, projection, _sender = allen_flow
    database = store.path
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    wal = database.parent / (database.name + "-wal")
    wal_before = hashlib.sha256(wal.read_bytes()).hexdigest() if wal.exists() else None
    projection.snapshot(graph_id)
    projection.changes_since(graph_id, 0)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    if wal_before is not None:
        assert hashlib.sha256(wal.read_bytes()).hexdigest() == wal_before
    # the read-only connection itself refuses to write
    from hyprial.daemon.impl.pac.storage.store import connect

    connection = connect(database, read_only=True)
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 - sqlite3.OperationalError: readonly
            connection.execute(
                "INSERT INTO graphs (graph_id, name, version, created_by, created_at) "
                "VALUES ('x', 'x', 1, 'x', 0)"
            )
    finally:
        connection.close()
    assert not hasattr(projection, "write") and not hasattr(projection, "save")


def test_incremental_fold_equals_full_snapshot(allen_flow) -> None:
    store, graph_id, projection, _sender = allen_flow
    full = projection.snapshot(graph_id)

    state = ProjectionState()
    structure = None
    cursor = 0
    while True:
        changes = projection.changes_since(graph_id, cursor)
        if not changes["events"]:
            break
        if structure is None or changes["version"] != structure["version"]:
            from hyprial.daemon.impl.pac.graphs.projection import _load_structure
            from hyprial.daemon.impl.pac.storage.store import connect

            connection = connect(store.path, read_only=True)
            try:
                structure = _load_structure(connection, graph_id)
            finally:
                connection.close()
        for event in changes["events"]:
            fold_event(state, structure, event)
        cursor = changes["cursor"]

    incremental = restate(structure, state.events)
    assert incremental["turns"] == full["turns"]
    assert incremental["withdraws"] == full["withdraws"]
    assert incremental["state"] == full["state"]
    assert len(incremental["steps"]) == len(full["steps"])


def test_new_event_updates_projection_incrementally_without_drift(allen_flow) -> None:
    """One more flag event lands; the projection moves exactly one step."""

    store, graph_id, projection, _sender = allen_flow
    before = projection.snapshot(graph_id)
    changes = projection.changes_since(graph_id, 0)
    assert changes["cursor"] == before["steps"][-1]["seq"]

    reactor = PacReactor(store, sender=NullSender())
    reactor.reset_flag(graph_id, "上线", actor=principal_uri("deploy"))  # the last step reopens
    after = projection.snapshot(graph_id)
    assert len(after["steps"]) == len(before["steps"]) + 1
    assert after["steps"][-1]["action"] == "reset"
    assert after["steps"][-1]["node"] == "上线"
    reopened = {
        node["node"]: node for node in after["state"]["nodes"] if node["turn"] == "current"
    }
    assert set(reopened) == {"上线"}
    # the earlier prefix is unchanged: no drift on the untouched steps
    assert after["steps"][:-1] == before["steps"]
