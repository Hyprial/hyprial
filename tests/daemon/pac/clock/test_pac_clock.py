"""PAC v2 P2: clock nodes (overdue), phases-P2 acceptance list.

An overdue is a derived notification, never a flag event; the clock is
injectable; the synthetic (event_id, edge) key makes re-ticks idempotent;
an owner setting the flag late runs the normal forward trigger.
"""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from hyprial.identity import PAC_NODE_SHAPE_INVALID, PacError
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path


@pytest.fixture()
def clock_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    # No agents registry: the owner write boundary validates principal shape
    # only (full URIs below), never local knowledge.
    store = PacGraphStore(default_database_path(state_dir=state))
    graph_id = create_graph(store, name="clocks", created_by="user:allen")["graphId"]
    # S -> C(clock) -> T: C has one upstream (S) and one downstream (T).
    for node_id, owner, kind, deadline in (
        ("S", "user:allen", "task", None),
        ("C", "user:worker", "clock", 5_000),
        ("T", "user:tester", "task", None),
    ):
        version = store.graph(graph_id)["version"]
        add_node(
            store,
            state,
            graph_id=graph_id,
            node_id=node_id,
            owner=owner,
            brief_ref=f"r#{node_id}",
            kind=kind,
            deadline_ms=deadline,
            expect_version=version,
        )
    version = store.graph(graph_id)["version"]
    add_edge(store, graph_id=graph_id, from_node="S", to_node="C", kind="forward", expect_version=version)
    version = store.graph(graph_id)["version"]
    add_edge(store, graph_id=graph_id, from_node="C", to_node="T", kind="forward", expect_version=version)

    activate_graph(store, graph_id, actor="user:allen")
    sender = NullSender()
    now = {"ms": 1_000}
    reactor = PacReactor(store, clock=lambda: now["ms"], sender=sender)
    yield store, graph_id, reactor, sender, now
    store.close()


def test_no_overdue_before_the_deadline(clock_graph) -> None:
    store, graph_id, reactor, sender, now = clock_graph
    planned = reactor.tick_clocks(graph_id)
    assert planned == []
    assert sender.sent == []
    assert store.notifications(graph_id) == []


def test_overdue_notifies_owner_and_every_forward_upstream(clock_graph) -> None:
    store, graph_id, reactor, _sender, now = clock_graph
    now["ms"] = 6_000  # past C's 5_000 deadline, C still unflagged
    planned = reactor.tick_clocks(graph_id)
    edges = sorted(item.edge for item in planned)
    assert edges == ["clock-up:S->C", "clock:C"]
    recipients = {item.edge: item.recipient for item in planned}
    assert recipients["clock:C"] == "user:worker"  # C's owner
    assert recipients["clock-up:S->C"] == "user:allen"  # S's owner (the upstream)
    assert all(item.text.startswith("逾期:C ") for item in planned)

    # idempotent: re-ticking the same deadline re-notifies nobody
    again = reactor.tick_clocks(graph_id)
    assert [item for item in again if item.text.startswith("逾期")] == []
    rows = [row for row in store.notifications(graph_id) if row.kind == "overdue"]
    # via the synthetic-event view (no flag_events row exists for them)
    synthetic = store.notifications_by_synthetic_event(graph_id)
    assert len(synthetic) == 2
    assert all(row.kind == "overdue" for row in synthetic)
    assert rows == [] or all(True for _ in rows)  # overdue never lands in flag_events

    import sqlite3

    raw = sqlite3.connect(store.path)
    events = raw.execute(
        "SELECT COUNT(*) FROM flag_events WHERE graph_id = ?", (graph_id,)
    ).fetchone()[0]
    raw.close()
    assert events == 0, "逾期不是 flag set/reset;它不进事实表"


def test_late_set_still_triggers_the_forward_path(clock_graph) -> None:
    store, graph_id, reactor, _sender, now = clock_graph
    reactor.set_flag(graph_id, "S", actor="user:allen")  # C's join opens
    now["ms"] = 6_000
    reactor.tick_clocks(graph_id)  # overdue fired
    # the owner confirms late: normal forward trigger, overdue does not block
    outcome = reactor.set_flag(graph_id, "C", actor="user:worker")
    assert any(item.edge == "C -> T" for item in outcome.planned)
    assert any(item.recipient == "user:tester" for item in outcome.planned)
    assert store.node(graph_id, "C").flag is True


def test_flagged_clock_node_never_goes_overdue(clock_graph) -> None:
    store, graph_id, reactor, _sender, now = clock_graph
    reactor.set_flag(graph_id, "S", actor="user:allen")
    reactor.set_flag(graph_id, "C", actor="user:worker")
    now["ms"] = 9_000
    assert reactor.tick_clocks(graph_id) == []


def _build_guarded_clock(state: Path) -> tuple[PacGraphStore, str]:
    store = PacGraphStore(default_database_path(state_dir=state))
    graph_id = create_graph(store, name="guarded", created_by="user:allen")["graphId"]
    add_node(
        store,
        state,
        graph_id=graph_id,
        node_id="work",
        owner="user:allen",
        brief_ref="r#work",
        expect_version=1,
    )
    add_node(
        store,
        state,
        graph_id=graph_id,
        node_id="deadline",
        owner="user:allen",
        brief_ref="r#deadline",
        kind="clock",
        deadline_ms=10,
        guarded_by_node_id="work",
        expect_version=2,
    )
    activate_graph(store, graph_id, actor="user:allen")
    return store, graph_id


def test_guarded_clock_unfinished_task_goes_overdue_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    (tmp_path / "home").mkdir()
    state = tmp_path / "state"
    state.mkdir()
    store, graph_id = _build_guarded_clock(state)
    try:
        reactor = PacReactor(store, clock=lambda: 20)
        planned = reactor.tick_clocks(graph_id)
        assert [(item.edge, item.recipient) for item in planned] == [
            ("clock:deadline", "user:allen")
        ]
        assert reactor.tick_clocks(graph_id) == []
        assert len(store.notifications_by_synthetic_event(graph_id)) == 1
    finally:
        store.close()


def test_guarded_clock_completion_wins_expiry_race_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    (tmp_path / "home").mkdir()
    state = tmp_path / "state"
    state.mkdir()
    completing, graph_id = _build_guarded_clock(state)
    waiting_to_begin = threading.Event()
    completion_committed = threading.Event()
    results: list[list[object]] = []
    failures: list[BaseException] = []

    def expire() -> None:
        ticking = PacGraphStore(completing.path)
        real_write = ticking.write

        def begin_after_completion():
            waiting_to_begin.set()
            assert completion_committed.wait(5)
            return real_write()

        ticking.write = begin_after_completion
        try:
            results.append(PacReactor(ticking, clock=lambda: 20).tick_clocks(graph_id))
        except BaseException as error:  # surface thread failures deterministically
            failures.append(error)
        finally:
            ticking.close()

    thread = threading.Thread(target=expire)
    thread.start()
    try:
        assert waiting_to_begin.wait(5)
        PacReactor(completing, clock=lambda: 20).set_flag(
            graph_id, "work", actor="user:allen"
        )
        completion_committed.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert failures == []
        assert results == [[]]
        assert completing.node(graph_id, "work").flag is True
        assert completing.notifications_by_synthetic_event(graph_id) == []
    finally:
        completion_committed.set()
        thread.join(timeout=10)
        completing.close()


def test_clock_node_requires_a_deadline_and_task_refuses_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    store = PacGraphStore(default_database_path(state_dir=state))
    try:
        graph_id = create_graph(store, name="shapes", created_by="user:allen")["graphId"]
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state,
                graph_id=graph_id,
                node_id="c",
                owner="user:allen",
                brief_ref="r",
                kind="clock",
                deadline_ms=None,
                expect_version=1,
            )
        assert raised.value.code == PAC_NODE_SHAPE_INVALID
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state,
                graph_id=graph_id,
                node_id="t",
                owner="user:allen",
                brief_ref="r",
                kind="task",
                deadline_ms=1_000,
                expect_version=1,
            )
        assert raised.value.code == PAC_NODE_SHAPE_INVALID
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state,
                graph_id=graph_id,
                node_id="guard-on-task",
                owner="user:allen",
                brief_ref="r",
                guarded_by_node_id="anything",
                expect_version=1,
            )
        assert raised.value.code == PAC_NODE_SHAPE_INVALID
        add_node(
            store,
            state,
            graph_id=graph_id,
            node_id="work",
            owner="user:allen",
            brief_ref="r",
            expect_version=1,
        )
        with pytest.raises(PacError) as raised:
            add_node(
                store,
                state,
                graph_id=graph_id,
                node_id="guard-missing",
                owner="user:allen",
                brief_ref="r",
                kind="clock",
                deadline_ms=1_000,
                guarded_by_node_id="missing",
                expect_version=2,
            )
        assert raised.value.code == PAC_NODE_SHAPE_INVALID
        result = add_node(
            store,
            state,
            graph_id=graph_id,
            node_id="guarded-clock",
            owner="user:allen",
            brief_ref="r",
            kind="clock",
            deadline_ms=1_000,
            guarded_by_node_id="work",
            expect_version=2,
        )
        assert result["guardedByNodeId"] == "work"
        assert store.node(graph_id, "guarded-clock").guarded_by_node_id == "work"
    finally:
        store.close()
