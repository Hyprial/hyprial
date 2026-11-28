"""The in-memory ``StateStore`` used by other packages' tests: injectable failures."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs.state import Batch, MemoryStateStore, StateStoreBroken

from .conftest import SPACE_ID, Writer, materialize


def _store() -> tuple[MemoryStateStore, Writer]:
    store = MemoryStateStore(SPACE_ID)
    writer = Writer(1)
    store.commit(Batch(appends=(("meta", writer.put("owner", "alice")),)))
    return store, writer


def test_a_failure_before_commit_applies_nothing() -> None:
    store, writer = _store()
    store.fail_next("commit", OSError(28, "no space"))
    with pytest.raises(OSError):
        store.commit(Batch(appends=(("meta", writer.put("acl", "x")), ("tree", Writer(2).put("a", 1)))))
    assert materialize(store.load("meta")) == {"m": {"owner": "alice"}, "t": ""}
    assert store.load("tree") is None
    store.commit(Batch(appends=(("tree", Writer(3).put("b", 1)),)))
    assert materialize(store.load("tree")) == {"m": {"b": 1}, "t": ""}


def test_a_failure_after_commit_leaves_the_batch_applied_and_the_store_broken() -> None:
    store, writer = _store()
    store.fail_next("commit.after")
    with pytest.raises(OSError):
        store.commit(Batch(appends=(("meta", writer.put("acl", "x")),)))
    with pytest.raises(StateStoreBroken):
        store.load("meta")
    reopened = MemoryStateStore(SPACE_ID, backing=store.backing)
    assert reopened.recovered_batch is True
    assert materialize(reopened.load("meta")) == {"m": {"owner": "alice", "acl": "x"}, "t": ""}
    assert MemoryStateStore(SPACE_ID, backing=store.backing).recovered_batch is False


def test_failures_are_one_shot_and_named() -> None:
    store, writer = _store()
    store.fail_next("compact")
    store.fail_next("load", RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        store.load("meta")
    assert store.load("meta") is not None
    with pytest.raises(OSError):
        store.compact("meta")
    store.compact("meta")
    assert materialize(store.load("meta")) == {"m": {"owner": "alice"}, "t": ""}
    with pytest.raises(ValueError):
        store.fail_next("unknown-point")


def test_reopening_shares_the_backing_and_counts_operations() -> None:
    store, writer = _store()
    store.commit(Batch(appends=(("meta", writer.put("b", 2)),)))
    assert store.backing.commits == 2
    reopened = MemoryStateStore(SPACE_ID, backing=store.backing)
    assert materialize(reopened.load("meta")) == {"m": {"owner": "alice", "b": 2}, "t": ""}
    assert reopened.backing.loads >= 1
    with pytest.raises(ValueError):
        MemoryStateStore("other-space", backing=store.backing)
