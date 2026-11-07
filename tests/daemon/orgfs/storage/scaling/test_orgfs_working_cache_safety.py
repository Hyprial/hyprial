"""Reusable working documents must not retain rolled-back mutations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import _doc_roots


_DOC = "doc-00000000-0000-0000-0000-000000000031"


def _store(path: Path) -> LocalSpaceStore:
    store = LocalSpaceStore(path, "space-cache", node_id="node-cache")
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["space"].__setitem__("spaceId", "space-cache"),
            doc["members"].__setitem__(
                "user:owner", Map({"mode": "rw", "removedAt": None})
            ),
        ),
        author="user:owner",
        actor=None,
    )
    store.commit(
        _DOC,
        lambda doc: doc.__setitem__("base", Text("yes")),
        author="user:owner",
        actor="agent:a",
    )
    return store


def test_failed_single_commit_discards_reused_working_doc(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:

        def fail(point: str) -> None:
            if point == "before_journal":
                raise RuntimeError("stop")

        store.fault_hook = fail
        with pytest.raises(RuntimeError, match="stop"):
            store.commit(
                _DOC,
                lambda doc: doc.__setitem__("poison", Text("no")),
                author="user:owner",
                actor="agent:a",
            )
        store.fault_hook = None
        store.commit(
            _DOC,
            lambda doc: doc.__setitem__("good", Text("yes")),
            author="user:owner",
            actor="agent:a",
        )
        doc = store._docs[_DOC]  # noqa: SLF001
        assert "poison" not in _doc_roots(doc)
        assert doc.get("good", type=Text).to_py() == "yes"
    finally:
        store.close()


def test_sql_rollback_discards_reused_working_doc(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store._db.set_authorizer(  # noqa: SLF001
            lambda action, table, _column, _database, _source: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and table == "commits"
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            store.commit(
                _DOC,
                lambda doc: doc.__setitem__("poison", Text("no")),
                author="user:owner",
                actor="agent:a",
            )
        store._db.set_authorizer(None)  # noqa: SLF001
        store.commit(
            _DOC,
            lambda doc: doc.__setitem__("good", Text("yes")),
            author="user:owner",
            actor="agent:a",
        )
        doc = store._docs[_DOC]  # noqa: SLF001
        assert "poison" not in _doc_roots(doc)
        assert doc.get("good", type=Text).to_py() == "yes"
    finally:
        store.close()
