"""ROUND 18 owner test, split out to keep test_member_add_index.py under the
structural line limit."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore

from collections.abc import Iterator

from .test_member_add_index import USERS, _genesis, _set_member, _store


@pytest.fixture
def stores() -> Iterator[list[LocalSpaceStore]]:
    opened: list[LocalSpaceStore] = []
    yield opened
    for store in reversed(opened):
        store.close()


def test_post_commit_frontier_materialization_defers_instead_of_raising(
    tmp_path: Path,
    stores: list[LocalSpaceStore],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ROUND 18 (hq review): the frontier cache is written AFTER the commit is
    journaled and applied.  If the frontier cannot be materialized then, the
    write must defer (journal-authoritative, rebuilt lazily); raising here
    turned an applied import into a reported rejection and skipped drain and
    projection."""

    store = _store(tmp_path, "defer-frontier", stores)
    _genesis(store)
    _set_member(store, USERS[0], active=True, serial=1)
    token = store._meta_frontier_for_commit()
    assert token
    with store._db:
        store._db.execute("DELETE FROM meta_frontiers WHERE token = ?", (token,))
    retained = store._db.execute("SELECT COUNT(*) FROM meta_frontiers").fetchone()[0]
    assert retained > 0
    monkeypatch.setattr(store, "_membership_at", lambda _token: None)

    with caplog.at_level("WARNING"):
        store._record_meta_frontier(token)  # must not raise

    assert "deferred metadata frontier materialization" in caplog.text
    assert store._db.execute("SELECT COUNT(*) FROM meta_frontiers").fetchone()[0] == retained
    assert store._db.execute(
        "SELECT 1 FROM meta_frontiers WHERE token = ?", (token,)
    ).fetchone() is None
