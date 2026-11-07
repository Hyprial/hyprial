"""§C/R7: public snapshots, cursor domains, history and immutable structure."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import hyprial.daemon.impl.pac.graphs.subscription as subscription
from tests.support.pac.builders import principal_uri
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.graphs.projection import Projection, audit
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore


@pytest.fixture()
def draft(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_OWNER", "owner")
    store = PacGraphStore(tmp_path / "pac-graph.sqlite3")
    gid = create_graph(store, name="public", created_by=principal_uri("owner"))["graphId"]
    version = 1
    for node in ("a", "b", "reject", "clock"):
        version = add_node(store, tmp_path, graph_id=gid, node_id=node, owner=principal_uri("owner"),
                           brief_ref=f"unreadable#ref-{node}", kind="clock" if node == "clock" else "task",
                           deadline_ms=1 if node == "clock" else None, expect_version=version)["version"]
    for source, target, kind in (("a", "b", "forward"), ("reject", "b", "back")):
        version = add_edge(store, graph_id=gid, from_node=source, to_node=target,
                           kind=kind, expect_version=version)["version"]
    yield store, gid
    store.close()


def test_draft_does_not_react_and_activation_freezes_structure(draft):
    store, gid = draft
    reactor = PacReactor(store, clock=lambda: 20)
    cursor = store.journal_events(gid)[-1]["seq"]
    with pytest.raises(PacError) as raised:
        reactor.set_flag(gid, "a", actor=principal_uri("owner"))
    assert raised.value.code == "PAC_GRAPH_NOT_ACTIVE"
    assert reactor.tick_clocks(gid) == []
    assert store.journal_events(gid, cursor) == []
    with pytest.raises(PacError) as raised:
        activate_graph(store, gid, actor=principal_uri("intruder"))
    assert raised.value.code == "PAC_GRAPH_NOT_OWNER"
    activate_graph(store, gid, actor=principal_uri("owner"))
    activate_graph(store, gid, actor=principal_uri("owner"))
    assert [e["type"] for e in store.journal_events(gid, cursor)] == ["graph_activated"]
    reactor.set_flag(gid, "a", actor=principal_uri("owner"))
    version = store.graph(gid)["version"]
    for operation in (
        lambda: add_node(store, store.path.parent, graph_id=gid, node_id="new", owner=principal_uri("owner"),
                         brief_ref="ref", expect_version=version),
        lambda: add_edge(store, graph_id=gid, from_node="a", to_node="clock", expect_version=version),
    ):
        with pytest.raises(PacError) as raised:
            operation()
        assert raised.value.code == "PAC_GRAPH_FROZEN"
    assert store.graph(gid)["version"] == version
    assert len(Projection(store.path).public_snapshot(gid)["notifications"]) == 1
    assert audit(store.path, gid)["ok"] is True


def test_activation_and_edit_recheck_inside_same_writer_lock(draft):
    store, gid = draft
    other = PacGraphStore(store.path)
    write = store.write

    def interleave():
        activate_graph(other, gid, actor=principal_uri("owner"))
        return write()

    store.write = interleave
    try:
        with pytest.raises(PacError) as raised:
            add_edge(store, graph_id=gid, from_node="b", to_node="clock",
                     expect_version=store.graph(gid)["version"])
        assert raised.value.code == "PAC_GRAPH_FROZEN"
        assert len(store.edges(gid)) == 2
    finally:
        other.close()


def test_snapshot_is_one_read_transaction_including_watermark(draft, monkeypatch):
    store, gid = draft
    activate_graph(store, gid, actor=principal_uri("owner"))
    before = Projection(store.path).public_snapshot(gid)
    other = PacGraphStore(store.path)
    original = subscription._structure

    def interleave(db, graph):
        result = original(db, graph)
        PacReactor(other, clock=lambda: 77).set_flag(gid, "a", actor=principal_uri("owner"), reason_ref="result#a")
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subscription, "_structure", interleave)
            during = Projection(store.path).public_snapshot(gid)
        assert during == before, "flags, plans, metadata and cursor must use the same snapshot"
        after = Projection(store.path).public_snapshot(gid)
        assert after["cursor"] > during["cursor"]
        assert after["flags"]["a"] == {"flag": True, "setBy": "user:owner", "setAt": 77,
                                       "reasonRef": "result#a", "completedCount": 1}
        changes = subscription.events_since(store.path, gid, during["cursor"], journal_id=during["journalId"])
        assert [e["type"] for e in changes["events"]] == ["flag_set", "notification_planned"]
    finally:
        other.close()


def test_draft_structure_snapshot_cannot_mix_two_versions(draft, monkeypatch):
    store, gid = draft
    before = Projection(store.path).public_snapshot(gid)
    other = PacGraphStore(store.path)
    original = subscription._structure

    def interleave(db, graph):
        add_node(other, store.path.parent, graph_id=gid, node_id="new", owner=principal_uri("owner"),
                 brief_ref="ref#new", expect_version=graph["version"])
        return original(db, graph)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subscription, "_structure", interleave)
            assert Projection(store.path).public_snapshot(gid) == before
        changes = subscription.events_since(store.path, gid, before["cursor"])["events"]
        assert len(changes) == 1
        assert changes[0]["type"] == "structure_changed" and changes[0]["data"]["resync"]
    finally:
        other.close()


def test_back_request_is_current_even_while_old_flag_is_set(draft):
    store, gid = draft
    activate_graph(store, gid, actor=principal_uri("owner"))
    reactor = PacReactor(store)
    reactor.set_flag(gid, "a", actor=principal_uri("owner"))
    reactor.set_flag(gid, "b", actor=principal_uri("owner"))
    assert Projection(store.path).public_snapshot(gid)["assignments"] == []
    reactor.set_flag(gid, "reject", actor=principal_uri("owner"))
    snapshot = Projection(store.path).public_snapshot(gid)
    assert snapshot["flags"]["b"]["flag"] is True
    assert snapshot["flags"]["b"]["completedCount"] == 1
    assert [(a["nodeId"], a["round"]) for a in snapshot["assignments"]] == [("b", 2)]
    identity = snapshot["assignments"][0]["activationId"]
    reactor.reset_flag(gid, "b", actor=principal_uri("owner"))
    assert Projection(store.path).public_snapshot(gid)["assignments"][0]["activationId"] == identity
    reactor.set_flag(gid, "b", actor=principal_uri("owner"))
    assert Projection(store.path).public_snapshot(gid)["assignments"] == []


def test_repeated_unfinished_requests_coalesce_and_withdraw_releases_only_its_edge(draft):
    store, gid = draft
    activate_graph(store, gid, actor=principal_uri("owner"))
    reactor = PacReactor(store)
    reactor.set_flag(gid, "a", actor=principal_uri("owner"))
    first = Projection(store.path).public_snapshot(gid)["assignments"][0]
    reactor.set_flag(gid, "reject", actor=principal_uri("owner"))
    both = Projection(store.path).public_snapshot(gid)["assignments"]
    assert len(both) == 1 and len(both[0]["requests"]) == 2
    assert both[0]["activationId"] == first["activationId"]
    reactor.reset_flag(gid, "a", actor=principal_uri("owner"))
    remaining = Projection(store.path).public_snapshot(gid)["assignments"]
    assert len(remaining) == 1 and len(remaining[0]["requests"]) == 1
    assert remaining[0]["requests"][0]["edge"] == "reject -> b"
    reactor.reset_flag(gid, "reject", actor=principal_uri("owner"))
    assert Projection(store.path).public_snapshot(gid)["assignments"] == []


@pytest.mark.parametrize("after", [-1, 10**12])
def test_invalid_cursor_requires_resync(draft, after):
    store, gid = draft
    with pytest.raises(PacError) as raised:
        subscription.events_since(store.path, gid, after)
    assert raised.value.code == "PAC_EVENTS_RESYNC"
    assert raised.value.data["resync"] is True


def test_journal_identity_prevents_reusing_cursor_from_replaced_database(draft):
    store, gid = draft
    with pytest.raises(PacError) as raised:
        subscription.events_since(store.path, gid, 0, journal_id="other-database")
    assert raised.value.code == "PAC_EVENTS_RESYNC"


def test_global_gaps_strict_suffix_and_bounded_pages(draft):
    store, gid = draft
    activate_graph(store, gid, actor=principal_uri("owner"))
    cursor = Projection(store.path).public_snapshot(gid)["cursor"]
    reactor = PacReactor(store)
    for i in range(130):
        reactor.set_flag(gid, "b", actor=principal_uri("owner"))
        if i == 0:
            create_graph(store, name="unrelated", created_by=principal_uri("owner"))
        reactor.reset_flag(gid, "b", actor=principal_uri("owner"))
    page1 = subscription.events_since(store.path, gid, cursor)
    assert len(page1["events"]) == 256
    page2 = subscription.events_since(store.path, gid, page1["cursor"], until=page1["highWatermark"])
    assert len(page2["events"]) == 4
    seqs = [e["seq"] for e in page1["events"] + page2["events"]]
    assert seqs == sorted(set(seqs)) and seqs[1] - seqs[0] == 2
    assert all(seq > cursor for seq in seqs)
    assert subscription.events_since(store.path, gid, seqs[-1])["events"] == []


@pytest.mark.parametrize("field,value", [
    ("recipient", "wrong"), ("kind", "withdraw"), ("round_no", 99),
    ("text", "wrong"), ("sender", "wrong"), ("at", -1), ("plan_json", "{}"),
])
def test_same_key_drift_compares_full_semantics(draft, field, value):
    store, gid = draft
    activate_graph(store, gid, actor=principal_uri("owner"))
    PacReactor(store).set_flag(gid, "a", actor=principal_uri("owner"))
    assert audit(store.path, gid)["ok"]
    with store.write() as db:
        db.execute(f"UPDATE notifications SET {field}=?", (value,))
    if field == "plan_json":
        # Retain its node identity while corrupting decision inputs.
        with store.write() as db:
            db.execute("UPDATE notifications SET plan_json=?", (json.dumps({"nodeId": "b"}),))
    result = audit(store.path, gid)
    assert result["ok"] is False and result["drift"]


def test_public_reads_are_read_only_and_return_refs_without_dereferencing(draft):
    store, gid = draft
    activate_graph(store, gid, actor=principal_uri("owner"))
    PacReactor(store).set_flag(gid, "a", actor=principal_uri("owner"), reason_ref="/nonexistent/result#reference")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in
              (store.path, Path(str(store.path) + "-wal")) if p.exists()}
    snapshot = Projection(store.path).public_snapshot(gid)
    subscription.events_since(store.path, gid, snapshot["cursor"])
    assert snapshot["flags"]["a"]["reasonRef"] == "/nonexistent/result#reference"
    assert snapshot["actors"] == [] and snapshot["closed"] is None
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_clock_ownership_uses_plan_identity_not_graph_name_prefix(draft):
    store, gid = draft
    child = create_graph(store, name=f"{gid}:child", created_by=principal_uri("owner"))["graphId"]
    add_node(store, store.path.parent, graph_id=child, node_id="clock", owner=principal_uri("owner"),
             brief_ref="ref#clock", kind="clock", deadline_ms=1, expect_version=1)
    activate_graph(store, child, actor=principal_uri("owner"))
    PacReactor(store, clock=lambda: 20).tick_clocks(child)
    assert len(Projection(store.path).public_snapshot(child)["notifications"]) == 1
    assert Projection(store.path).public_snapshot(gid)["notifications"] == []
    assert store.notifications_by_synthetic_event(gid) == []


def test_published_json_schema_validates_actual_snapshot_and_journal(draft):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[4] / 'contract/pac/events/schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    store, gid = draft
    activate_graph(store, gid, actor=principal_uri("owner"))
    reactor = PacReactor(store)
    reactor.set_flag(gid, "a", actor=principal_uri("owner"), reason_ref="result#a")
    reactor.reset_flag(gid, "a", actor=principal_uri("owner"))
    reactor.tick_clocks(gid)
    snapshot = Projection(store.path).public_snapshot(gid)
    validator.validate(snapshot)
    for event in subscription.events_since(store.path, gid, 0)["events"]:
        validator.validate(event)


def test_pac_runtime_does_not_name_a_specific_external_consumer():
    root = Path(__file__).resolve().parents[4] / "src/hyprial/pac"
    forbidden = "kan" + "ban"
    assert all(forbidden not in path.read_text().lower() for path in root.glob("*.py"))


def test_legacy_history_is_not_reinterpreted_and_old_cursor_requires_snapshot(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript((Path(__file__).resolve().parents[4] / 'tests/fixtures/pac/slice-1-schema.sql').read_text())
    db.execute("INSERT INTO graphs VALUES ('g','legacy',4,'owner',1)")
    db.execute("INSERT INTO nodes VALUES ('g','a','owner','ref#a','task',NULL,1,'owner',2,'result#a')")
    db.execute("INSERT INTO nodes VALUES ('g','b','owner','ref#b','task',NULL,0,NULL,NULL,NULL)")
    db.execute("INSERT INTO flag_events VALUES (1,'e','g',3,'a','set','owner',2,'result#a')")
    # The edge was added AFTER the flag. No turn was ever planned (P1).
    db.execute("INSERT INTO edges VALUES ('g','a','b','forward')")
    db.commit()
    db.close()
    store = PacGraphStore(path)
    try:
        snapshot = Projection(path).public_snapshot("g")
        assert snapshot["active"] is not None
        assert snapshot["assignments"] == [] and snapshot["notifications"] == []
        assert snapshot["flags"]["a"]["reasonRef"] == "result#a"
        with pytest.raises(PacError) as raised:
            subscription.events_since(path, "g", snapshot["cursorFloor"] - 1)
        assert raised.value.code == "PAC_EVENTS_RESYNC"
        assert subscription.events_since(path, "g", snapshot["cursor"], journal_id=snapshot["journalId"])["events"] == []
        assert audit(path, "g")["ok"] is False, "legacy drift must not masquerade as a good replay"
    finally:
        store.close()
