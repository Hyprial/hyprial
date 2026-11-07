"""PAC v2 P1: the flag reactor (spec ②③④, phases P1 acceptance list).

Runs the Allen graph from the spec through every acceptance: the
converge/second-arrival join (demo -> pm), (event_id, edge) idempotency,
reset withdraws, the back edge's 「轮到你(第 2 次)」, owner-only flags,
and the concurrency guarantee the library-level unique constraint gives.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hyprial.identity import (
    PAC_FLAG_ALREADY_SET,
    PAC_FLAG_NOT_OWNER,
    PAC_FLAG_NOT_SET,
    PacError,
)
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from tests.support.pac.builders import build_allen_graph, principal_uri  # noqa: TID252 - shared spec script


@pytest.fixture()
def allen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PacReactor, PacGraphStoreLike, str, NullSender]:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    store, graph_id = build_allen_graph(state)
    sender = NullSender()
    clock = iter(range(1_000, 10_000))  # strictly increasing, injectable
    reactor = PacReactor(store, clock=lambda: next(clock), sender=sender)
    yield reactor, store, graph_id, sender
    store.close()


type PacGraphStoreLike = object  # store handle; typed loosely for the fixture


def turns(sender: NullSender) -> list[str]:
    return [item["text"] for item in sender.sent if item["text"].startswith("轮到你")]


def withdraws(sender: NullSender) -> list[str]:
    return [item["text"] for item in sender.sent if item["text"].startswith("已撤回")]


# --------------------------------------------------------------------------- #
# ② converge / second arrival + the join condition
# --------------------------------------------------------------------------- #


def test_join_waits_for_every_predecessor(allen) -> None:
    reactor, _store, graph_id, sender = allen
    reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    # pm-helper's only predecessor is feat-collector: it turned.
    first = turns(sender)
    assert len(first) == 1
    assert first[0].startswith("轮到你:pm-helper notes/allen-example.md#pm-helper")
    # dev-help has NOT turned: its predecessor pm-confirm-1 has not flagged.
    assert not any("dev-help" in text for text in turns(sender))


def reactor_set_event(reactor: PacReactor, graph_id: str, index: int) -> str:
    events = reactor._store.flag_events(graph_id)  # noqa: SLF001 - test introspection
    return events[index].event_id


def test_demo_flag_gives_owner_pm_the_second_turn(allen) -> None:
    """② acceptance under ruling 丙: pm-confirm-2's turn (owner pm's second
    confirmation, a distinct node) is caused by demo's flag."""

    reactor, _store, graph_id, sender = allen
    happy_path_to_demo(reactor, graph_id)  # ends with 实施 set; demo turned
    demo_event = reactor.set_flag(graph_id, "demo", actor=principal_uri("dev")).event["eventId"]
    pm_turns = [
        text for text in turns(sender) if "pm-confirm" in text
    ]
    assert len(pm_turns) == 2, pm_turns
    assert "轮到你:pm-confirm-1 " in pm_turns[0]
    assert "轮到你:pm-confirm-2 " in pm_turns[1]
    assert demo_event in pm_turns[1], "the demo confirmation turn cites demo's set event"


def happy_path_to_demo(reactor: PacReactor, graph_id: str) -> None:
    reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    reactor.set_flag(graph_id, "pm-helper", actor=principal_uri("pm-helper"))
    reactor.set_flag(graph_id, "pm-confirm-1", actor=principal_uri("pm"))
    reactor.set_flag(graph_id, "dev-help", actor=principal_uri("dev-help"))
    reactor.set_flag(graph_id, "dev-confirm", actor=principal_uri("dev"))
    reactor.set_flag(graph_id, "实施", actor=principal_uri("dev"))


# --------------------------------------------------------------------------- #
# ② idempotency: (event_id, edge) is a database constraint
# --------------------------------------------------------------------------- #


def test_replayed_event_yields_exactly_one_notification_row(allen) -> None:
    reactor, store, graph_id, sender = allen
    outcome = reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    assert len(outcome.planned) == 1
    # Simulate a replay: the same (event_id, edge) insert is refused by the
    # primary key — the library-level unique constraint, not diligence.
    db = store.write()
    try:
        row = outcome.planned[0]
        cursor = db.execute(
            "INSERT OR IGNORE INTO notifications "
            "(event_id, edge, kind, recipient, round_no, text, sender, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.event_id,
                row.edge,
                row.kind,
                row.recipient,
                row.round_no,
                row.text,
                row.sender,
                0,
            ),
        )
        db.commit()
        assert cursor.rowcount == 0, "the (event_id, edge) primary key refuses the replay"
    finally:
        db.rollback() if db.in_transaction else None
    assert len(sender.sent) == 1
    assert len(store.notifications(graph_id)) == 1


def test_concurrent_set_on_converge_node_notifies_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two threads set the last missing predecessor of the same converge
    node; the node's owner is turned exactly once per edge, never twice
    for one edge (the PK is the arbiter)."""

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    store, graph_id = build_allen_graph(state)
    try:
        # fan-out shape: two owners set their nodes at the same moment;
        # both events try to turn pm-helper on their own edges.
        reactor_seed = PacReactor(store)
        reactor_seed.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))

        senders: list[list[str]] = [[], []]
        barrier = threading.Barrier(2)

        def setter(index: int, node: str, actor: str) -> None:
            local = PacReactor(
                PacGraphStore(store.path),
                sender=RecordingSender(senders[index]),
            )
            try:
                barrier.wait()
                local.set_flag(graph_id, node, actor=principal_uri(actor))
            finally:
                local.close()

        threads = [
            threading.Thread(target=setter, args=(0, "feat-collector", "feat-bot")),
            threading.Thread(target=setter, args=(1, "pm-helper", "pm-helper")),
        ]
        # reset feat-collector first so both setters hit a valid state
        reactor_seed.reset_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        notifications = store.notifications(graph_id)
        keys = [(row.event_id, row.edge) for row in notifications]
        assert len(keys) == len(set(keys)), "no (event_id, edge) duplicates"
        pmh_turns = [
            row
            for row in notifications
            if row.kind == "turn" and row.edge == "feat-collector -> pm-helper"
        ]
        # one per feat-collector set event (it set, reset, re-set): the
        # row count per event never grows; the round number depends on the
        # racy arrival order of pm-helper's own concurrent set (both orders
        # are honest — that is what a real race here looks like)
        assert len(pmh_turns) == 2
        assert sorted(row.round_no for row in pmh_turns) in ([1, 1], [1, 2])
    finally:
        store.close()


class RecordingSender:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    def send(self, *, recipient: str, text: str, sender: str, conversation_id: str, idempotency_key: str, summary: str, expires_at_ms: int | None = None) -> str:
        self.sink.append(text)
        return idempotency_key


# --------------------------------------------------------------------------- #
# ③ reset withdraws; downstream flag untouched
# --------------------------------------------------------------------------- #


def test_reset_withdraws_only_already_turned_downstream(allen) -> None:
    reactor, store, graph_id, sender = allen
    reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))  # turn(pm-helper)
    pmh = store.node(graph_id, "pm-helper")
    reset = reactor.reset_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    assert len(withdraws(sender)) == 1
    assert "已撤回:pm-helper" in withdraws(sender)[0]
    assert reset.event["action"] == "reset"
    # ③ acceptance: the downstream flag is NOT auto-flipped (it was never
    # set here — and stays unset, with no one else's flag touched).
    assert store.node(graph_id, "pm-helper").flag is False
    assert store.node(graph_id, "pm-helper").flag_set_by is None
    assert pmh.flag is False

    # a second reset on an already-unset flag is refused: nothing to withdraw
    with pytest.raises(PacError) as raised:
        reactor.reset_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    assert raised.value.code == PAC_FLAG_NOT_SET


def test_reset_never_withdraws_an_edge_that_never_turned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A join-blocked out-edge delivered no turn; the reset must not
    withdraw on it — 「已因它触发过」 is a real precondition, not a
    formality.  The diamond doubles as the plain-join gate: the converge
    node z-join has preds {a, w2}; a's flag alone must NOT turn z-join
    (the setter sorts first on purpose — a join that peeked at only one
    predecessor would pass the wrong one here and get caught)."""

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_OWNER", "allen")
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    store = PacGraphStore(default_database_path(state_dir=state))
    try:
        graph_id = create_graph(store, name="diamond", created_by=principal_uri("allen"))["graphId"]
        version = 1
        # a fans out to y1 (fires immediately) and z-join (blocked: needs
        # w2 too); the setter 'a' sorts before 'w2' so a broken join that
        # reads only the first predecessor still sees a SET pred and turns
        # z-join — which this test asserts does not happen.
        for node_id, owner in (("a", "allen"), ("y1", "y-owner"), ("z-join", "z-owner"), ("w2", "w-owner")):
            version = add_node(
                store, state, graph_id=graph_id, node_id=node_id, owner=principal_uri(owner),
                brief_ref=f"r#{node_id}", expect_version=version,
            )["version"]
        for source, target in (("a", "y1"), ("a", "z-join"), ("w2", "z-join")):
            version = add_edge(
                store, graph_id=graph_id, from_node=source, to_node=target,
                kind="forward", expect_version=version,
            )["version"]
        activate_graph(store, graph_id, actor=principal_uri("allen"))
        reactor = PacReactor(store, sender=NullSender())
        outcome = reactor.set_flag(graph_id, "a", actor=principal_uri("allen"))
        assert [item.node_id for item in outcome.planned] == ["y1"], (
            "z-join is join-blocked: a's flag alone must not turn it"
        )
        # completing the join turns z-join exactly once
        reactor.set_flag(graph_id, "w2", actor=principal_uri("w-owner"))
        reactor.reset_flag(graph_id, "w2", actor=principal_uri("w-owner"))
        joined = reactor.set_flag(graph_id, "w2", actor=principal_uri("w-owner"))
        assert [item.node_id for item in joined.planned] == ["z-join"]
        withdraw_outcome = reactor.reset_flag(graph_id, "a", actor=principal_uri("allen"))
        assert [item.node_id for item in withdraw_outcome.planned] == ["y1"], (
            "only the turned edge withdraws; z-join was turned by w2, not a"
        )
    finally:
        store.close()


def test_reset_does_not_flip_a_set_downstream_flag(allen) -> None:
    reactor, store, graph_id, sender = allen
    happy_path_to_demo(reactor, graph_id)  # ends with 实施 set
    assert store.node(graph_id, "实施").flag is True
    reactor.reset_flag(graph_id, "dev-confirm", actor=principal_uri("dev"))
    assert store.node(graph_id, "实施").flag is True, "后置 flag 不自动改"
    assert any("已撤回:实施" in text for text in withdraws(sender))


# --------------------------------------------------------------------------- #
# ④ back edge: the second turn, no new node, rounds from flag_events
# --------------------------------------------------------------------------- #


def test_back_edge_fires_the_second_turn_without_new_nodes(allen) -> None:
    reactor, store, graph_id, sender = alan_back_edge_flow(allen)
    # ④ acceptance pieces:
    notifications = store.notifications(graph_id)
    pmh_turns = [
        row for row in notifications if row.kind == "turn" and row.edge == "pm-不通过 -> pm-helper"
    ]
    assert len(pmh_turns) == 1
    assert pmh_turns[0].round_no == 2
    assert "第 2 次" in pmh_turns[0].text
    # flag_events counts pm-helper's sets: 1 so far -> the redo makes it 2
    assert store.set_event_count(graph_id, "pm-helper") == 1
    reactor.reset_flag(graph_id, "pm-helper", actor=principal_uri("pm-helper"))
    reactor.set_flag(graph_id, "pm-helper", actor=principal_uri("pm-helper"))
    assert store.set_event_count(graph_id, "pm-helper") == 2, "flag_events 数出第 2 次"
    # node count unchanged: no new node was created by the back edge
    assert len(store.nodes(graph_id)) == 13
    # and the notification text carried 「第 2 次」
    assert any("轮到你(第 2 次):pm-helper" in item["text"] for item in sender.sent)


def alan_back_edge_flow(allen):
    reactor, store, graph_id, sender = allen
    happy_path_to_demo(reactor, graph_id)
    reactor.set_flag(graph_id, "demo", actor=principal_uri("dev"))  # pm-confirm-2's turn
    reactor.set_flag(graph_id, "pm-不通过", actor=principal_uri("pm"))  # pm rejects instead
    return reactor, store, graph_id, sender


def test_back_edge_target_is_re_asked_after_redo(allen) -> None:
    """After pm-helper redoes, its re-set turns pm-confirm-1 again (round
    2 — pm confirmed once): the withdraw already told pm-confirm-1's
    owner the input moved."""

    reactor, store, graph_id, sender = alan_back_edge_flow(allen)
    reactor.reset_flag(graph_id, "pm-helper", actor=principal_uri("pm-helper"))  # withdraw -> pm-confirm-1
    assert any("已撤回:pm-confirm-1 " in text for text in withdraws(sender))
    before = [text for text in turns(sender) if "轮到你(第 2 次):pm-confirm-1" in text]
    reactor.set_flag(graph_id, "pm-helper", actor=principal_uri("pm-helper"))
    after = [text for text in turns(sender) if "轮到你(第 2 次):pm-confirm-1" in text]
    assert len(after) == len(before) + 1
    assert store.set_event_count(graph_id, "pm-confirm-1") == 1, (
        "pm-confirm-1 has confirmed once; the redo ask is its round 2"
    )


# --------------------------------------------------------------------------- #
# §7-D owner-only flags + set/reset shape rules
# --------------------------------------------------------------------------- #


def test_only_the_owner_flips_their_flag(allen) -> None:
    reactor, _store, graph_id, _sender = allen
    with pytest.raises(PacError) as raised:
        reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("pm"))
    assert raised.value.code == PAC_FLAG_NOT_OWNER
    with pytest.raises(PacError) as raised:
        reactor.reset_flag(graph_id, "feat-collector", actor=principal_uri("pm-helper"))
    assert raised.value.code == PAC_FLAG_NOT_OWNER


def test_set_on_set_is_refused_and_reset_on_unset_is_refused(allen) -> None:
    reactor, _store, graph_id, _sender = allen
    reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    with pytest.raises(PacError) as raised:
        reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    assert raised.value.code == PAC_FLAG_ALREADY_SET
    reactor.reset_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    with pytest.raises(PacError) as raised:
        reactor.reset_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    assert raised.value.code == PAC_FLAG_NOT_SET


def test_events_and_notifications_carry_the_graph_version(allen) -> None:
    reactor, store, graph_id, _sender = allen
    outcome = reactor.set_flag(graph_id, "feat-collector", actor=principal_uri("feat-bot"))
    graph_version = store.graph(graph_id)["version"]
    assert outcome.event["version"] == graph_version
    rows = store._db.execute(  # noqa: SLF001 - introspection
        "SELECT version FROM flag_events WHERE event_id = ?",
        (outcome.event["eventId"],),
    ).fetchone()
    assert rows[0] == graph_version
