from __future__ import annotations

import pytest
from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore

_OPEN = []
TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _close_stores():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _genesis(store):
    return store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["docs"].__setitem__(
                "tree", Map({"active": TREE_DOC_ID, "planId": None})
            ),
        ),
        author="user:owner",
        actor=None,
    )


@pytest.mark.parametrize(
    "crash_point",
    [
        "before_journal",
        "after_journal",
        "before_state",
        "after_state",
        "before_broadcast",
        "after_broadcast",
    ],
)
def test_reopen_after_commit_boundary_has_no_duplicate_and_replays_journal(
    tmp_path, crash_point
):
    """review-p1 15k: all six commit/broadcast crash points recover consistently."""

    state = tmp_path / "state"
    first = FileSpaceStore(state, "space-1", node_id="node-a")
    _OPEN.append(first)
    _genesis(first)

    def crash(point):
        if point == crash_point:
            raise RuntimeError(f"injected crash at {point}")

    # A reopened process gets a fresh incarnation. Establish the target
    # writer lane on the same process before installing the crash hook.
    crashing = FileSpaceStore(state, "space-1", node_id="node-a")
    _OPEN.append(crashing)
    crashing.commit(
        "meta",
        lambda doc: doc["writers"].__setitem__("manual", Map({"node": "node-a"})),
        author="user:owner",
        actor="agent:x",
    )
    if crash_point in {
        "before_journal",
        "after_journal",
        "before_state",
        "after_state",
    }:
        crashing.fault_hook = crash
        with pytest.raises(RuntimeError, match=crash_point):
            crashing.commit(
                TREE_DOC_ID,
                lambda doc: doc.__setitem__("x", Text("x")),
                author="user:owner",
                actor="agent:x",
            )
    else:
        record = crashing.commit(
            TREE_DOC_ID,
            lambda doc: doc.__setitem__("x", Text("x")),
            author="user:owner",
            actor="agent:x",
        )
        crashing.fault_hook = crash
        with pytest.raises(RuntimeError, match=crash_point):
            crashing.mark_broadcast(record.writer, record.seq, doc_id=TREE_DOC_ID)
    reopened = FileSpaceStore(state, "space-1", node_id="node-a")
    _OPEN.append(reopened)
    rows = reopened._db.execute(
        "SELECT doc_id, seq FROM commits WHERE doc_id=?", (TREE_DOC_ID,)
    ).fetchall()
    if crash_point == "before_journal":
        assert rows == []
    else:
        assert len(rows) == 1
        assert reopened.frontier(TREE_DOC_ID)
    if crash_point == "before_broadcast":
        assert any(
            __import__("json").loads(item.envelope_bytes)["docId"] == TREE_DOC_ID
            for item in reopened.unbroadcast()
        )
    if crash_point == "after_broadcast":
        assert all(
            __import__("json").loads(item.envelope_bytes)["docId"] != TREE_DOC_ID
            for item in reopened.unbroadcast()
        )
    assert (
        len(tuple(reopened.unbroadcast()))
        == reopened._db.execute(
            "SELECT COUNT(*) FROM commits WHERE broadcast_at IS NULL"
        ).fetchone()[0]
    )


def test_unbroadcast_returns_original_bytes_and_holder_frontier_settles(tmp_path):
    """review-p1 15k: unbroadcast replay is byte-identical and holder VV settles it."""
    state = FileSpaceStore(tmp_path / "state", "space-1", node_id="node-a")
    _OPEN.append(state)
    _genesis(state)
    record = state.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("x", Text("x")),
        author="user:owner",
        actor="agent:x",
    )
    assert state.unconfirmed_commits() == 3
    journal_bytes = bytes(
        state._db.execute(
            "SELECT envelope_bytes FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
            (record.writer, record.seq, TREE_DOC_ID),
        ).fetchone()[0]
    )
    assert (
        next(
            item.envelope_bytes
            for item in state.unbroadcast()
            if item.writer == record.writer
            and item.seq == record.seq
            and __import__("json").loads(item.envelope_bytes)["docId"] == TREE_DOC_ID
        )
        == journal_bytes
        == record.envelope_bytes
    )
    assert journal_bytes != state._docs[TREE_DOC_ID].get_update()
    state.mark_broadcast(record.writer, record.seq, doc_id=TREE_DOC_ID)
    assert (
        any(
            item.envelope_bytes == record.envelope_bytes for item in state.unbroadcast()
        )
        is False
    )
    state.record_holder_frontier(
        "node-b", False, {TREE_DOC_ID: state.frontier(TREE_DOC_ID)}
    )
    assert state.unconfirmed_commits() == 3
    state.record_holder_frontier(
        "node-b",
        True,
        {TREE_DOC_ID: state.frontier(TREE_DOC_ID), "meta": state.frontier("meta")},
    )
    assert state.unconfirmed_commits() == 0


@pytest.mark.parametrize(
    "crash_point", ["before_journal", "after_journal", "before_state", "after_state"]
)
def test_commit_many_crash_points_replay_the_whole_batch(tmp_path, crash_point):
    """n4: every commit_many crash point recovers zero or the complete batch."""

    state = tmp_path / f"batch-{crash_point}"
    store = FileSpaceStore(state, "space-1", node_id="node-a")
    _OPEN.append(store)
    _genesis(store)
    store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("registered", Text("yes")),
        author="user:owner",
        actor="agent:batch",
    )

    def crash(point):
        if point == crash_point:
            raise RuntimeError(f"injected crash at {point}")

    store.fault_hook = crash
    with pytest.raises(RuntimeError, match=crash_point):
        store.commit_many(
            (
                (
                    "doc-00000000-0000-0000-0000-000000000041",
                    lambda doc: doc.__setitem__("a", Text("one")),
                ),
                (
                    "doc-00000000-0000-0000-0000-000000000042",
                    lambda doc: doc.__setitem__("b", Text("two")),
                ),
            ),
            author="user:owner",
            actor="agent:batch",
        )

    reopened = FileSpaceStore(state, "space-1", node_id="node-a")
    _OPEN.append(reopened)
    rows = reopened._db.execute(
        "SELECT doc_id FROM commits WHERE doc_id LIKE 'doc-%' ORDER BY doc_id"
    ).fetchall()
    if crash_point == "before_journal":
        assert rows == []
    else:
        assert [str(row[0]) for row in rows] == [
            "doc-00000000-0000-0000-0000-000000000041",
            "doc-00000000-0000-0000-0000-000000000042",
        ]
        assert reopened._docs[rows[0][0]].get("a", type=Text).to_py() == "one"
        assert reopened._docs[rows[1][0]].get("b", type=Text).to_py() == "two"
