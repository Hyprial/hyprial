from __future__ import annotations


import pytest
from pycrdt import Map

from hyprial.daemon.impl.orgfs.storage.store import (
    FileSpaceStore)

_OPEN = []
TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _close_stores():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _store(tmp_path, name, node, *, blob_store=None):
    store = FileSpaceStore(
        tmp_path / name, "space-1", node_id=node, blob_store=blob_store
    )
    original_commit = store.commit

    def commit(doc_id, mutate, *, author, actor):
        if (
            doc_id != "meta"
            or store._db.execute(
                "SELECT 1 FROM commits WHERE doc_id = 'meta' LIMIT 1"
            ).fetchone()
        ):
            return original_commit(doc_id, mutate, author=author, actor=actor)

        def with_physical_tree_mapping(doc):
            mutate(doc)
            doc.get("docs", type=Map)["tree"] = Map(
                {
                    "active": TREE_DOC_ID,
                    "activatedAt": "2026-09-26T00:00:00Z",
                    "planId": None,
                }
            )

        return original_commit(
            doc_id, with_physical_tree_mapping, author=author, actor=actor
        )

    store.commit = commit
    _OPEN.append(store)
    return store


def _setup(tmp_path):
    owner = _store(tmp_path, "owner", "node-owner")
    owner.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:rw", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:ro", Map({"mode": "ro"})),
        ),
        author="user:owner",
        actor=None,
    )
    owner.commit(
        "meta",
        lambda doc: doc["members"]["user:rw"].__setitem__(
            "removedAt", "2026-09-26T00:00:00Z"
        ),
        author="user:owner",
        actor=None,
    )
    return owner


def _envelopes(store):
    return [
        bytes(row[0])
        for row in store._db.execute(
            "SELECT envelope_bytes FROM commits ORDER BY rowid"
        )
    ]












































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'TREE_DOC_ID',
    '_OPEN',
    '_close_stores',
    '_envelopes',
    '_setup',
    '_store',
]
