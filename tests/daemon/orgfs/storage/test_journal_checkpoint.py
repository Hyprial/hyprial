"""Startup replay starts at the .loro checkpoint, not rowid zero."""

from __future__ import annotations

from pathlib import Path

from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore

SPACE = "checkpoint-space"
TREE = "tree-00000000-0000-0000-0000-000000000001"


def _seed(store: LocalSpaceStore) -> None:
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].update(
                {"owner": "user:owner", "spaceId": SPACE, "name": "team"}
            ),
            doc["members"].__setitem__(
                "user:owner", Map({"mode": "rw", "removedAt": None})
            ),
            doc["docs"].__setitem__(
                "tree", Map({"active": TREE, "planId": None})
            ),
        ),
        author="user:owner",
        actor=None,
    )


def test_replay_starts_after_the_loro_checkpoint(tmp_path: Path) -> None:
    store = LocalSpaceStore(tmp_path, SPACE, node_id="node-a")
    try:
        _seed(store)
        for index in range(2):
            store.commit(
                TREE,
                lambda doc, index=index: doc.__setitem__(f"k{index}", Text(str(index))),
                author="user:owner",
                actor="agent:owner:a",
            )
        store.checkpoint()
        doc = store._doc(TREE)
        path = store._doc_path(TREE)
        saved_doc = path.read_bytes()
        saved_version = doc.get_state()
        saved_rowid = int(
            store._db.execute(
                "SELECT rowid FROM doc_checkpoints WHERE doc_id = ?", (TREE,)
            ).fetchone()[0]
        )
        for index in range(2, 5):
            store.commit(
                TREE,
                lambda doc, index=index: doc.__setitem__(f"k{index}", Text(str(index))),
                author="user:owner",
                actor="agent:owner:a",
            )
        expected_state = store._doc(TREE).get_state()
    finally:
        store.close()

    # Simulate the persisted .loro/checkpoint pair from after the first two
    # commits while the journal keeps all five rows.
    path.write_bytes(saved_doc)
    import sqlite3

    db = sqlite3.connect(tmp_path / "orgfs" / "spaces" / SPACE / "journal.sqlite3")
    try:
        db.execute(
            "UPDATE doc_checkpoints SET rowid = ?, version = ? WHERE doc_id = ?",
            (saved_rowid, saved_version, TREE),
        )
        db.commit()
    finally:
        db.close()

    decoded: list[str] = []
    real_decode = LocalSpaceStore._decode_envelope

    def counting_decode(self, raw):
        envelope = real_decode(self, raw)
        decoded.append(str(envelope["docId"]))
        return envelope

    LocalSpaceStore._decode_envelope = counting_decode
    try:
        reopened = LocalSpaceStore(tmp_path, SPACE, node_id="node-a")
        try:
            assert decoded.count(TREE) == 3
            assert reopened._doc(TREE).get_state() == expected_state
        finally:
            reopened.close()
    finally:
        LocalSpaceStore._decode_envelope = real_decode


def _checkpoint_fixture(tmp_path: Path) -> tuple[Path, bytes, bytes, int, bytes]:
    store = LocalSpaceStore(tmp_path, SPACE, node_id="node-a")
    try:
        _seed(store)
        for index in range(2):
            store.commit(
                TREE,
                lambda doc, index=index: doc.__setitem__(f"k{index}", Text(str(index))),
                author="user:owner",
                actor="agent:owner:a",
            )
        store.checkpoint()
        path = store._doc_path(TREE)
        saved_doc = path.read_bytes()
        saved_version = store._doc(TREE).get_state()
        saved_rowid = int(
            store._db.execute(
                "SELECT rowid FROM doc_checkpoints WHERE doc_id = ?", (TREE,)
            ).fetchone()[0]
        )
        for index in range(2, 5):
            store.commit(
                TREE,
                lambda doc, index=index: doc.__setitem__(f"k{index}", Text(str(index))),
                author="user:owner",
                actor="agent:owner:a",
            )
        expected_state = store._doc(TREE).get_state()
    finally:
        store.close()
    return path, saved_doc, saved_version, saved_rowid, expected_state


def _reopen_counting(tmp_path: Path, path: Path, saved_doc: bytes, *, version: bytes, rowid: int, expected_state: bytes) -> None:
    path.write_bytes(saved_doc)
    import sqlite3

    db = sqlite3.connect(tmp_path / "orgfs" / "spaces" / SPACE / "journal.sqlite3")
    try:
        db.execute(
            "UPDATE doc_checkpoints SET rowid = ?, version = ? WHERE doc_id = ?",
            (rowid, version, TREE),
        )
        db.commit()
    finally:
        db.close()

    decoded: list[str] = []
    real_decode = LocalSpaceStore._decode_envelope

    def counting_decode(self, raw):
        envelope = real_decode(self, raw)
        decoded.append(str(envelope["docId"]))
        return envelope

    LocalSpaceStore._decode_envelope = counting_decode
    try:
        reopened = LocalSpaceStore(tmp_path, SPACE, node_id="node-a")
        try:
            assert decoded.count(TREE) == 5
            assert reopened._doc(TREE).get_state() == expected_state
        finally:
            reopened.close()
    finally:
        LocalSpaceStore._decode_envelope = real_decode


def test_checkpoint_version_mismatch_falls_back_to_full_replay(tmp_path: Path) -> None:
    path, saved_doc, _saved_version, saved_rowid, expected_state = _checkpoint_fixture(
        tmp_path
    )
    # The .loro is after two commits but the checkpoint claims a different state.
    _reopen_counting(
        tmp_path,
        path,
        saved_doc,
        version=b"stale-version",
        rowid=saved_rowid,
        expected_state=expected_state,
    )


def test_checkpoint_rowid_beyond_max_falls_back_to_full_replay(tmp_path: Path) -> None:
    path, saved_doc, saved_version, _saved_rowid, expected_state = _checkpoint_fixture(
        tmp_path
    )
    _reopen_counting(
        tmp_path,
        path,
        saved_doc,
        version=saved_version,
        rowid=99999,
        expected_state=expected_state,
    )
