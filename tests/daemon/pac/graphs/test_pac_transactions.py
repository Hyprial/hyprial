"""Slice-2 A.5.6: real SQLite connections, lock boundaries and crash recovery."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hyprial.daemon.impl.pac.graphs.flags as flags_module
import hyprial.daemon.impl.pac.graphs.reactor as reactor_module
from hyprial.identity import PAC_FLAG_ALREADY_SET, PAC_FLAG_NOT_OWNER, PacError
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.storage.journal import activation_id
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore


@pytest.fixture()
def graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HYPRIAL_OWNER", "owner")
    store = PacGraphStore(tmp_path / "pac-graph.sqlite3")
    gid = create_graph(store, name="join", created_by="user:owner")["graphId"]
    version = 1
    for node in ("a", "b", "c"):
        version = add_node(store, tmp_path, graph_id=gid, node_id=node,
                           owner="user:owner", brief_ref=f"brief#{node}",
                           expect_version=version)["version"]
    for node in ("a", "b"):
        version = add_edge(store, graph_id=gid, from_node=node, to_node="c",
                           kind="forward", expect_version=version)["version"]
    add_node(store, tmp_path, graph_id=gid, node_id="clock", owner="user:owner",
             brief_ref="ref#clock", kind="clock", deadline_ms=10, expect_version=version)
    activate_graph(store, gid, actor="user:owner")
    yield store, gid
    store.close()


def test_two_connections_same_flag_validate_under_writer_lock(graph):
    store, gid = graph
    barrier = threading.Barrier(2)

    def setter():
        local = PacGraphStore(store.path)
        real_write = local.write
        first = True

        def synchronized_write():
            nonlocal first
            if first:
                first = False
                barrier.wait(timeout=5)
            return real_write()

        local.write = synchronized_write
        try:
            PacReactor(local).set_flag(gid, "a", actor="user:owner")
            return "set"
        except PacError as error:
            return error.code
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(setter) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == sorted(["set", PAC_FLAG_ALREADY_SET])
    assert store.set_event_count(gid, "a") == 1
    assert [e["type"] for e in store.journal_events(gid) if e["type"].startswith("flag_")] == ["flag_set"]


def test_two_connections_join_plans_before_releasing_writer_lock(graph):
    store, gid = graph
    ready = threading.Barrier(2)
    planning = threading.Event()
    attempted = threading.Event()
    committed = threading.Event()

    def first():
        local = PacGraphStore(store.path)
        reactor = PacReactor(local, clock=lambda: 100)
        real_plan = reactor._plan_set

        def pause_in_plan(*args):
            planning.set()
            assert attempted.wait(5)
            return real_plan(*args)

        reactor._plan_set = pause_in_plan
        try:
            ready.wait(timeout=5)
            return reactor.set_flag(gid, "a", actor="user:owner", reason_ref="ref#a")
        finally:
            committed.set()
            local.close()

    def second():
        local = PacGraphStore(store.path)
        local._db.execute("PRAGMA busy_timeout = 0")
        reactor = PacReactor(local, clock=lambda: 200)
        blocked = False
        try:
            ready.wait(timeout=5)
            assert planning.wait(5)
            try:
                reactor.set_flag(gid, "b", actor="user:owner", reason_ref="ref#b")
            except sqlite3.OperationalError as error:
                assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                blocked = True
            finally:
                attempted.set()
            assert committed.wait(5)
            if blocked:
                reactor.set_flag(gid, "b", actor="user:owner", reason_ref="ref#b")
            return blocked
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        a.result(timeout=10)
        blocked = b.result(timeout=10)
    rows = store.notifications(gid)
    assert len(rows) == 1, "different event/edge keys must not double-fire a join"
    assert blocked, "second connection must be locked out while first plans"
    assert rows[0].plan == {
        "graphId": gid, "version": store.graph(gid)["version"], "nodeId": "c",
        "activationId": activation_id(gid, "c", 1),
        "predecessors": [
            {"nodeId": "a", "flag": True, "flagSetAt": 100,
             "flagSetBy": "user:owner", "flagReasonRef": "ref#a"},
            {"nodeId": "b", "flag": True, "flagSetAt": 200,
             "flagSetBy": "user:owner", "flagReasonRef": "ref#b"},
        ],
    }
    journal = [e for e in store.journal_events(gid) if e["type"] not in {"structure_changed", "graph_activated"}]
    assert [e["type"] for e in journal] == ["flag_set", "flag_set", "notification_planned"]
    assert journal[-1]["data"]["plan"] == rows[0].plan
    PacReactor(store).reset_flag(gid, "a", actor="user:owner")
    assert store.notifications(gid)[0].plan == rows[0].plan, "inputs stay historical"


@pytest.mark.parametrize("changed", ["owner", "version"])
def test_prelock_changes_are_revalidated(graph, changed):
    store, gid = graph
    other = PacGraphStore(store.path)
    real_write = store.write

    def interleave():
        db = other.write()
        if changed == "owner":
            db.execute("UPDATE nodes SET owner='user:other' WHERE graph_id=? AND node_id='a'", (gid,))
        else:
            db.execute("UPDATE graphs SET version=version+1 WHERE graph_id=?", (gid,))
        db.commit()
        return real_write()

    store.write = interleave
    try:
        if changed == "owner":
            with pytest.raises(PacError) as raised:
                PacReactor(store).set_flag(gid, "a", actor="user:owner")
            assert raised.value.code == PAC_FLAG_NOT_OWNER
            assert not store.flag_events(gid)
        else:
            outcome = PacReactor(store).set_flag(gid, "a", actor="user:owner")
            assert outcome.event["version"] == store.graph(gid)["version"]
            assert store.journal_events(gid)[-1]["version"] == outcome.event["version"]
    finally:
        other.close()


@pytest.mark.parametrize("boundary", ["flag_journal", "plan", "notification_journal", "after_plan"])
def test_exception_rolls_back_flag_plan_and_journal(graph, monkeypatch, boundary):
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor="user:owner")
    before = store.journal_events(gid)
    reactor = PacReactor(store, sender=NullSender())
    append = reactor_module.append_event
    insert = reactor._insert_notifications

    def fail():
        raise RuntimeError("crash injected")

    def append_then_fail(*args, **kwargs):
        result = append(*args, **kwargs)
        if kwargs["type"] == {"flag_journal": "flag_set", "notification_journal": "notification_planned"}.get(boundary):
            fail()
        return result

    def insert_then_fail(*args, **kwargs):
        insert(*args, **kwargs)
        fail()

    monkeypatch.setattr(reactor_module, "append_event", append_then_fail)
    monkeypatch.setattr(flags_module, "append_event", append_then_fail)
    if boundary == "plan":
        monkeypatch.setattr(reactor, "_plan_set", lambda *args: fail())
    if boundary == "after_plan":
        monkeypatch.setattr(reactor, "_insert_notifications", insert_then_fail)
    with pytest.raises(RuntimeError, match="crash injected"):
        reactor.set_flag(gid, "b", actor="user:owner")
    reopened = PacGraphStore(store.path)
    try:
        assert not reopened.node(gid, "b").flag
        assert reopened.set_event_count(gid, "b") == 0
        assert not reopened.notifications(gid)
        assert reopened.journal_events(gid) == before
        assert not reactor._sender.sent
    finally:
        reopened.close()


@pytest.mark.parametrize("boundary", ["plan", "after_plan"])
def test_process_exit_mid_transaction_rolls_back(graph, boundary):
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor="user:owner")
    code = '''
import os, sys
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
r = PacReactor(PacGraphStore(sys.argv[1]))
if sys.argv[3] == "plan":
    r._plan_set = lambda *args: os._exit(73)
else:
    insert = r._insert_notifications
    def crash(*args, **kwargs):
        insert(*args, **kwargs)
        os._exit(73)
    r._insert_notifications = crash
r.set_flag(sys.argv[2], "b", actor="user:owner")
'''
    result = subprocess.run([sys.executable, "-c", code, str(store.path), gid, boundary],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 73, result.stderr
    reopened = PacGraphStore(store.path)
    try:
        assert not reopened.node(gid, "b").flag
        assert reopened.set_event_count(gid, "b") == 0
        assert not reopened.notifications(gid)
        assert [e["type"] for e in reopened.journal_events(gid) if e["type"].startswith("flag_")] == ["flag_set"]
    finally:
        reopened.close()


@pytest.mark.parametrize("boundary", ["before_send", "before_mark"])
def test_recovery_retries_original_plan_not_current_structure(graph, monkeypatch, boundary):
    store, gid = graph
    PacReactor(store).set_flag(gid, "a", actor="user:owner")
    sender = NullSender()
    reactor = PacReactor(store, sender=sender)

    def crash(*args, **kwargs):
        raise RuntimeError("crash injected")

    monkeypatch.setattr(reactor, "_deliver" if boundary == "before_send" else "_mark_delivered", crash)
    with pytest.raises(RuntimeError, match="crash injected"):
        reactor.set_flag(gid, "b", actor="user:owner")
    row = store.notifications(gid)[0]
    assert row.message_id is None
    assert store.node(gid, "b").flag
    # This direct mutation simulates a later structure version. Recovery must
    # resend only the recorded bytes even before R7's freeze guard lands.
    db = store.write()
    db.execute("UPDATE nodes SET brief_ref='changed' WHERE graph_id=?", (gid,))
    db.execute("UPDATE graphs SET version=version+1 WHERE graph_id=?", (gid,))
    db.commit()
    reopened = PacGraphStore(store.path)
    try:
        recover = PacReactor(reopened, sender=sender)
        assert recover.resend_undelivered(gid)["delivered"] == 1
        assert sender.sent[-1]["text"] == row.text
        if boundary == "before_mark":
            assert sender.sent[0] == sender.sent[1], "wire idempotency key survives lost ack"
        assert recover.resend_undelivered(gid)["delivered"] == 0
        assert reopened.notifications(gid)[0].plan == row.plan
        assert reopened.journal_events(gid)[-1]["type"] == "delivery_changed"
    finally:
        reopened.close()


def test_replaying_notification_plan_does_not_append_journal_twice(graph):
    store, gid = graph
    reactor = PacReactor(store)
    reactor.set_flag(gid, "a", actor="user:owner")
    outcome = reactor.set_flag(gid, "b", actor="user:owner")
    before = store.journal_events(gid)
    with store.write() as db:
        assert reactor._insert_notifications(
            list(outcome.planned), 999, db=db, graph_id=gid,
            version=store.graph(gid)["version"],
        ) == []
    assert store.journal_events(gid) == before
    assert len(store.notifications(gid)) == 1


def test_delivery_update_and_journal_rollback_together(graph, monkeypatch):
    store, gid = graph
    reactor = PacReactor(store)
    reactor.set_flag(gid, "a", actor="user:owner")
    reactor.set_flag(gid, "b", actor="user:owner")
    row = store.notifications(gid)[0]
    before = store.journal_events(gid)
    append = reactor_module.append_event

    def fail_delivery(*args, **kwargs):
        append(*args, **kwargs)
        raise RuntimeError("delivery journal crash")

    with monkeypatch.context() as patch:
        patch.setattr(reactor_module, "append_event", fail_delivery)
        with pytest.raises(RuntimeError, match="delivery journal crash"):
            reactor._mark_delivered(row.event_id, row.edge, "accepted-message")
    assert store.notifications(gid)[0].message_id is None
    assert store.journal_events(gid) == before
    reactor._mark_delivered(row.event_id, row.edge, "accepted-message")
    changes = store.journal_events(gid, before[-1]["seq"])
    assert [e["type"] for e in changes] == ["delivery_changed"]
    reactor._mark_delivered(row.event_id, row.edge, "accepted-message")
    assert store.journal_events(gid, before[-1]["seq"]) == changes


def test_clock_notification_is_journaled_without_flag_event(graph):
    store, gid = graph
    cursor = store.journal_events(gid)[-1]["seq"]
    reactor = PacReactor(store, clock=lambda: 20)
    planned = reactor.tick_clocks(gid)
    assert len(planned) == 1
    assert not store.flag_events(gid)
    assert not store.node(gid, "clock").flag
    journal = store.journal_events(gid, cursor)
    assert [e["type"] for e in journal] == ["notification_planned"]
    assert journal[0]["data"]["kind"] == "overdue"
    assert journal[0]["data"]["plan"]["nodeId"] == "clock"
    assert reactor.tick_clocks(gid) == []
    assert store.journal_events(gid, cursor) == journal


def test_sender_runs_after_commit_without_writer_lock(graph):
    store, gid = graph
    other = PacGraphStore(store.path)
    other._db.execute("PRAGMA busy_timeout = 0")
    sender = NullSender()
    send = sender.send

    def assert_unlocked(**kwargs):
        db = other.write()
        assert other.node(gid, "b").flag
        assert len(other.notifications(gid)) == 1
        assert other.journal_events(gid)[-1]["type"] == "notification_planned"
        db.rollback()
        return send(**kwargs)

    sender.send = assert_unlocked
    try:
        reactor = PacReactor(store, sender=sender)
        reactor.set_flag(gid, "a", actor="user:owner")
        assert len(reactor.set_flag(gid, "b", actor="user:owner").delivered) == 1
    finally:
        other.close()
