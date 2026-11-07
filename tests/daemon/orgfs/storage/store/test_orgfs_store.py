from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
import threading

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs.storage.store import (
    ORGFS_INLINE_UPDATE_BYTES,
    ORGFS_PAGE_BYTES,
    PYCRDT_CLIENT_ID_MAX,
    FileSpaceStore,
    LocalSpaceStore,
    StoreError,
    assert_client_ids_registered)
from hyprial.identity import ProtectedDocIdTooLongError, protected_directory_doc_id

_OPEN: list[LocalSpaceStore] = []
TREE_DOC_ID = "tree-00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _close_stores():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _store(tmp_path, name: str, node: str) -> LocalSpaceStore:
    store = FileSpaceStore(tmp_path / name, "space-1", node_id=node)
    _OPEN.append(store)
    return store


def _genesis(store: LocalSpaceStore):
    return store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["space"].__setitem__("spaceId", "space-1"),
            doc["members"].__setitem__(
                "user:owner", Map({"mode": "rw", "removedAt": None})
            ),
            doc["docs"].__setitem__(
                "tree",
                Map({"active": TREE_DOC_ID, "activatedAt": "genesis", "planId": None}),
            ),
        ),
        author="user:owner",
        actor=None,
    )


def test_commit_persists_frozen_layout_and_replays_raw_envelopes(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    first = _store(tmp_path, "state", "node-a")
    _genesis(first)
    change = first.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("note", Text("hello")),
        author="user:owner",
        actor="agent:a",
    )
    rows = first._db.execute(
        "SELECT doc_id, writer, seq, envelope_bytes FROM commits ORDER BY rowid"
    ).fetchall()
    assert [row["doc_id"] for row in rows] == ["meta", "meta", TREE_DOC_ID]
    assert [row["seq"] for row in rows] == [0, 0, 0]
    assert all(bytes(row["envelope_bytes"]) for row in rows)
    assert change.envelope_bytes == bytes(rows[-1]["envelope_bytes"])
    assert (first.root / "journal.sqlite3").exists()
    assert (first.root / "tree.loro").exists()
    assert (first.root / "meta.loro").exists()

    reopened = _store(tmp_path, "state", "node-a")
    assert reopened.frontier(TREE_DOC_ID) == first.frontier(TREE_DOC_ID)
    assert reopened._docs[TREE_DOC_ID].get("note", type=Text).to_py() == "hello"
    # The same writer incarnation is process-local and must not be reused.
    assert reopened.incarnation != first.incarnation


def test_max_length_doc_id_uses_a_bounded_checkpoint_temp_name(tmp_path) -> None:
    store = _store(tmp_path, "state", "node-a")
    _genesis(store)
    candidates = []
    for length in range(1, 201):
        try:
            candidate = protected_directory_doc_id(
                "space-1",
                f"directory/devices/owner/{'a' * length}.json",
                space_owner="user:owner",
            )
        except ProtectedDocIdTooLongError:
            break
        assert candidate is not None
        candidates.append(candidate)
    doc_id = max(candidates, key=len)
    assert len(doc_id) >= 235

    store.commit(
        doc_id,
        lambda doc: doc.__setitem__("text", Text("persisted")),
        author="user:owner",
        actor=None,
    )

    assert store._doc_path(doc_id).is_file()


def test_import_is_idempotent_and_rejects_same_key_byte_conflict(tmp_path, caplog):
    """review-p1 fix 14: conflicting journal bytes are rejected and warned."""
    source = _store(tmp_path, "source", "node-a")
    _genesis(source)
    source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("note", Text("hello")),
        author="user:owner",
        actor="agent:a",
    )
    target = _store(tmp_path, "target", "node-b")
    envelopes = [
        bytes(row[0])
        for row in source._db.execute(
            "SELECT envelope_bytes FROM commits ORDER BY rowid"
        )
    ]
    for envelope in envelopes:
        assert target.import_envelope(envelope, supplier="node-a").status == "applied"
    assert target.import_envelope(envelopes[-1], supplier="node-a").status == "applied"
    conflict = json.loads(envelopes[-1])
    conflict["origin"]["actor"] = "agent:tampered"
    conflicting_bytes = json.dumps(
        conflict, sort_keys=True, separators=(",", ":")
    ).encode()
    with caplog.at_level(logging.WARNING, logger='hyprial.daemon.impl.orgfs.storage.store'):
        result = target.import_envelope(conflicting_bytes, supplier="node-a")
    assert result.status == "rejected"
    assert result.code == "log-key-conflict"
    assert "same journal key with different bytes" in caplog.text
    assert target.frontier(TREE_DOC_ID) == source.frontier(TREE_DOC_ID)
    assert target._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 3


def test_export_uses_original_bytes_and_opaque_cursor(tmp_path):
    """review-p1 fix 14: an export cursor is bound to its version vector."""
    source = _store(tmp_path, "source", "node-a")
    _genesis(source)
    for value in ("one", "two", "three"):
        source.commit(
            TREE_DOC_ID,
            lambda doc, value=value: doc.__setitem__(value, Text(value)),
            author="user:owner",
            actor=value,
        )
    expected = [
        bytes(row[0])
        for row in source._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
            (TREE_DOC_ID,),
        )
    ]
    page = source.export_since(
        TREE_DOC_ID, b"", cursor=None, max_bytes=max(len(expected[0]) * 2, 1)
    )
    assert page.envelopes == (expected[0],)
    assert page.next is not None
    second = source.export_since(TREE_DOC_ID, b"", cursor=page.next, max_bytes=10_000)
    assert second.envelopes == tuple(expected[1:])
    with pytest.raises(StoreError, match="bad-cursor"):
        source.export_since(
            TREE_DOC_ID, source.frontier(TREE_DOC_ID), cursor=page.next, max_bytes=10_000
        )
    with pytest.raises(StoreError, match="bad-cursor"):
        source.export_since(TREE_DOC_ID, b"", cursor="bad", max_bytes=10_000)


def test_large_update_uses_blob_hook(tmp_path):
    """review-p1 fix 5: serialized updates above 512000 bytes use updateBlob."""

    class BlobSink:
        def __init__(self):
            self.values = {}

        def put(self, space_id, data, *, reason):
            digest = __import__("hashlib").sha256(data).hexdigest()
            self.values[(space_id, digest)] = data
            return digest

        def get(self, space_id, digest):
            return self.values[(space_id, digest)]

    blobs = BlobSink()
    store = FileSpaceStore(
        tmp_path / "state", "space-1", node_id="node-a", blob_store=blobs
    )
    _OPEN.append(store)
    _genesis(store)
    payload = "x" * (ORGFS_INLINE_UPDATE_BYTES + 2_000)
    record = store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("large", Text(payload)),
        author="user:owner",
        actor="agent:large",
    )
    envelope = json.loads(record.envelope_bytes)
    assert "updateBlob" in envelope
    assert "update" not in envelope
    assert len(blobs.values) == 1


def test_exact_inline_boundary_fits_page_and_first_envelope_is_always_admitted(
    tmp_path,
):
    """review-p1 fix 5: 512000-byte update fits and a page always admits one envelope."""

    store = _store(tmp_path, "state", "node-a")
    _genesis(store)
    actor = "agent:boundary"
    store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("seed", Text("s")),
        author="user:owner",
        actor=actor,
    )
    writer = store._writer_for("user:owner", actor)
    baseline = store._docs[TREE_DOC_ID].get_update()
    client_id = store._client_id(writer)

    def update_size(length: int) -> int:
        probe = Doc(client_id=client_id)
        probe.apply_update(baseline)
        before = probe.get_state()
        probe["boundary"] = Text("x" * length)
        store._advance_commit_clock(probe, writer=writer, seq=1)
        return len(probe.get_update(before))

    low, high = ORGFS_INLINE_UPDATE_BYTES - 256, ORGFS_INLINE_UPDATE_BYTES + 256
    while low <= high:
        middle = (low + high) // 2
        size = update_size(middle)
        if size < ORGFS_INLINE_UPDATE_BYTES:
            low = middle + 1
        elif size > ORGFS_INLINE_UPDATE_BYTES:
            high = middle - 1
        else:
            payload_length = middle
            break
    else:  # pragma: no cover - pycrdt update encoding should advance byte-for-byte here
        pytest.fail("could not construct an exact 512000-byte pycrdt update")

    before = store.frontier(TREE_DOC_ID)
    record = store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("boundary", Text("x" * payload_length)),
        author="user:owner",
        actor=actor,
    )
    envelope = json.loads(record.envelope_bytes)
    assert "update" in envelope
    assert len(base64.b64decode(envelope["update"])) == ORGFS_INLINE_UPDATE_BYTES
    page = store.export_since(TREE_DOC_ID, before, cursor=None, max_bytes=ORGFS_PAGE_BYTES)
    encoded = json.dumps(
        {"envelopes": [base64.b64encode(item).decode() for item in page.envelopes]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert len(encoded) <= ORGFS_PAGE_BYTES
    tiny = store.export_since(TREE_DOC_ID, before, cursor=None, max_bytes=1)
    assert tiny.envelopes == (record.envelope_bytes,)


@pytest.mark.parametrize("failure", ["too-large", "blob-put", "sql-insert"])
def test_failed_commit_never_poisoned_in_memory_document(tmp_path, failure):
    """review-p1 fix 4: every post-mutate pre-commit failure rolls back the live doc."""

    class BlobFailure:
        def put(self, space_id, data, *, reason):
            raise OSError("blob unavailable")

    blob_store = BlobFailure() if failure == "blob-put" else None
    store = FileSpaceStore(
        tmp_path / failure,
        "space-1",
        node_id="node-a",
        blob_store=blob_store,
    )
    _OPEN.append(store)
    _genesis(store)
    before = store._doc(TREE_DOC_ID).get_update()
    if failure == "sql-insert":
        store._db.execute(
            "CREATE TRIGGER fail_commit BEFORE INSERT ON commits "
            f"WHEN NEW.doc_id = '{TREE_DOC_ID}' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        store._db.commit()
        payload = "small"
        expected = Exception
    else:
        payload = "x" * (ORGFS_INLINE_UPDATE_BYTES + 1)
        expected = (StoreError, OSError)
    with pytest.raises(expected):
        store.commit(
            TREE_DOC_ID,
            lambda doc: doc.__setitem__("poison", Text(payload)),
            author="user:owner",
            actor="agent:failure",
        )
    assert store._docs[TREE_DOC_ID].get_update() == before
    assert (
        store._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = ?", (TREE_DOC_ID,)
        ).fetchone()[0]
        == 0
    )


def test_missing_update_blob_is_rejected_with_digest_then_retry_applies(tmp_path):
    """review-p1 fix 6: unknown updateBlob rejects with digest and retry is idempotent."""

    class Blobs:
        def __init__(self):
            self.values = {}

        def put(self, space_id, data, *, reason):
            digest = hashlib.sha256(data).hexdigest()
            self.values[(space_id, digest)] = data
            return digest

        def get(self, space_id, digest):
            return self.values[(space_id, digest)]

    source_blobs = Blobs()
    source = FileSpaceStore(
        tmp_path / "source", "space-1", node_id="node-a", blob_store=source_blobs
    )
    target_blobs = Blobs()
    target = FileSpaceStore(
        tmp_path / "target", "space-1", node_id="node-b", blob_store=target_blobs
    )
    _OPEN.extend((source, target))
    _genesis(source)
    record = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__(
            "large", Text("x" * (ORGFS_INLINE_UPDATE_BYTES + 2_000))
        ),
        author="user:owner",
        actor="agent:large",
    )
    for envelope in [
        bytes(row[0])
        for row in source._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' ORDER BY rowid"
        )
    ]:
        target.import_envelope(envelope, supplier="node-a")
    digest = json.loads(record.envelope_bytes)["updateBlob"]
    rejected = target.import_envelope(record.envelope_bytes, supplier="node-a")
    assert (rejected.status, rejected.code, rejected.details) == (
        "rejected",
        "unknown-blob",
        {"digest": digest},
    )
    assert (
        target._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = ?", (TREE_DOC_ID,)
        ).fetchone()[0]
        == 0
    )
    target_blobs.values[("space-1", digest)] = source_blobs.values[("space-1", digest)]
    assert (
        target.import_envelope(record.envelope_bytes, supplier="node-a").status
        == "applied"
    )
    assert (
        target.import_envelope(record.envelope_bytes, supplier="node-a").code
        == "duplicate"
    )


def test_mark_broadcast_is_scoped_by_document(tmp_path):
    """review-p1 fix 7: equal writer/seq values on two docs are marked independently."""

    store = _store(tmp_path, "state", "node-a")
    genesis = _genesis(store)
    tree = store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("x", Text("tree")),
        author="user:owner",
        actor=None,
    )
    assert (genesis.writer, genesis.seq) == (tree.writer, tree.seq)
    store.mark_broadcast(tree.writer, tree.seq, doc_id=TREE_DOC_ID)
    remaining = {
        (item.writer, item.seq, json.loads(item.envelope_bytes)["docId"])
        for item in store.unbroadcast()
    }
    assert (genesis.writer, genesis.seq, "meta") in remaining
    assert (tree.writer, tree.seq, TREE_DOC_ID) not in remaining


def test_overwrite_commits_are_covered_for_export_and_confirmation(tmp_path):
    """review-p1 fix 8: deletion-carrying overwrites become covered by a caught-up VV."""

    source = _store(tmp_path, "source", "node-a")
    _genesis(source)
    source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("value", Text("one")),
        author="user:owner",
        actor="agent:x",
    )
    source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("value", Text("two")),
        author="user:owner",
        actor="agent:x",
    )
    caught_up = source.frontier(TREE_DOC_ID)
    assert (
        source.export_since(TREE_DOC_ID, caught_up, cursor=None, max_bytes=10_000).envelopes
        == ()
    )
    source.record_holder_frontier(
        "node-b",
        True,
        {"meta": source.frontier("meta"), TREE_DOC_ID: caught_up},
    )
    assert source.unconfirmed_commits() == 0


def test_writer_client_id_is_per_writer_and_uses_the_53_bit_library_width(tmp_path):
    """review-p1 fix 10: writer attribution records each actual 53-bit CRDT client id."""

    assert Doc(client_id=PYCRDT_CLIENT_ID_MAX).client_id == PYCRDT_CLIENT_ID_MAX
    with pytest.raises(ValueError, match="53 bits"):
        Doc(client_id=PYCRDT_CLIENT_ID_MAX + 1)
    store = _store(tmp_path, "state", "Node_A:1")
    _genesis(store)
    first = store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("a", Text("a")),
        author="user:owner",
        actor="agent:a",
    )
    second = store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("b", Text("b")),
        author="user:owner",
        actor="agent:b",
    )
    assert first.writer != second.writer
    writers = store._docs["meta"].get("writers", type=Map).to_py()
    assert writers[first.writer]["peerId"] == store._client_id(first.writer)
    assert writers[second.writer]["peerId"] == store._client_id(second.writer)
    assert writers[first.writer]["peerId"] != writers[second.writer]["peerId"]


def test_snapshot_manifest_contains_all_frozen_attribution_fields(tmp_path):
    """review-p1 fix 11: snapshot manifests expose authorsBefore/createdBy/createdAt."""

    store = _store(tmp_path, "state", "node-a")
    _genesis(store)
    record = store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("x", Text("x")),
        author="user:owner",
        actor="agent:x",
    )
    manifest = store.snapshot(TREE_DOC_ID, shallow_since=None).to_dict()
    assert manifest["authorsBefore"] == {"user:owner": 1}
    assert manifest["createdBy"] == {
        "writer": record.writer,
        "author": "user:owner",
        "actor": "agent:x",
    }
    assert manifest["createdAt"].endswith("Z")


def test_empty_vv_clone_and_incremental_catchup_are_byte_identical(tmp_path):
    """review-p1 15i: empty-VV clone and incremental store catch-up are byte-identical."""

    source = _store(tmp_path, "source", "node-a")
    _genesis(source)
    for key, value in (("a", "one"), ("b", "two"), ("a", "three")):
        source.commit(
            TREE_DOC_ID,
            lambda doc, key=key, value=value: doc.__setitem__(key, Text(value)),
            author="user:owner",
            actor="agent:x",
        )
    metadata = source.export_since("meta", b"", cursor=None, max_bytes=10_000).envelopes
    changes = source.export_since(TREE_DOC_ID, b"", cursor=None, max_bytes=10_000).envelopes
    clone = _store(tmp_path, "clone", "node-clone")
    incremental = _store(tmp_path, "incremental", "node-incremental")
    for envelope in metadata:
        assert clone.import_envelope(envelope, supplier="node-a").status == "applied"
        assert (
            incremental.import_envelope(envelope, supplier="node-a").status == "applied"
        )
    for envelope in changes:
        assert clone.import_envelope(envelope, supplier="node-a").status == "applied"
    split = max(1, len(changes) // 2)
    for envelope in changes[:split]:
        assert (
            incremental.import_envelope(envelope, supplier="node-a").status == "applied"
        )
    page = source.export_since(
        TREE_DOC_ID, incremental.frontier(TREE_DOC_ID), cursor=None, max_bytes=10_000
    )
    for envelope in page.envelopes:
        assert (
            incremental.import_envelope(envelope, supplier="node-a").status == "applied"
        )
    assert (
        clone.frontier(TREE_DOC_ID)
        == incremental.frontier(TREE_DOC_ID)
        == source.frontier(TREE_DOC_ID)
    )
    assert clone._docs[TREE_DOC_ID].get_update() == incremental._docs[TREE_DOC_ID].get_update()


def test_delete_only_commit_advances_clock_and_is_not_covered_early(tmp_path):
    """review-p1-r2 D.1: a delete-only store commit stays missing until imported."""

    source = _store(tmp_path, "d1-source", "node-a")
    _genesis(source)
    source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("body", Text("non-empty")),
        author="user:owner",
        actor="agent:a",
    )
    target = _store(tmp_path, "d1-target", "node-b")
    envelopes = [
        bytes(row[0])
        for row in source._db.execute(
            "SELECT envelope_bytes FROM commits ORDER BY rowid"
        )
    ]
    for envelope in envelopes:
        assert target.import_envelope(envelope, supplier="node-a").status == "applied"
    before_delete = target.frontier(TREE_DOC_ID)
    deletion = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.get("body", type=Text).clear(),
        author="user:owner",
        actor="agent:a",
    )

    source.record_holder_frontier(
        "node-b", True, {"meta": target.frontier("meta"), TREE_DOC_ID: before_delete}
    )
    assert source.unconfirmed_commits() >= 1
    page = source.export_since(TREE_DOC_ID, before_delete, cursor=None, max_bytes=100_000)
    assert deletion.envelope_bytes in page.envelopes
    assert (
        target.import_envelope(deletion.envelope_bytes, supplier="node-a").status
        == "applied"
    )
    source.record_holder_frontier(
        "node-b",
        True,
        {"meta": target.frontier("meta"), TREE_DOC_ID: target.frontier(TREE_DOC_ID)},
    )
    assert source.unconfirmed_commits() == 0
    assert target._docs[TREE_DOC_ID].get("body", type=Text).to_py() == ""


def test_single_commit_noop_skips_marker_unless_it_creates_document(tmp_path):
    """n3: a marker never turns an existing-doc no-op into a journal commit."""

    store = _store(tmp_path, "n3", "node-a")
    _genesis(store)
    before = store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    with pytest.raises(StoreError, match="mutation produced no update"):
        store.commit(TREE_DOC_ID, lambda _doc: None, author="user:owner", actor=None)
    assert store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == before

    created = store.commit(
        "doc-00000000-0000-0000-0000-000000000003",
        lambda _doc: None,
        author="user:owner",
        actor=None,
    )
    assert json.loads(created.envelope_bytes)["docId"].startswith("doc-")
    assert store._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == before + 1


def test_failed_registration_attempt_cannot_poison_next_meta_commit(
    tmp_path, monkeypatch
):
    """n8: attempt-local writer registration ids are cleared before retry."""

    store = _store(tmp_path, "n8", "node-a")
    _genesis(store)
    external = Doc(client_id=424242)
    external.apply_update(store._docs["meta"].get_update())
    before = external.get_state()
    external.get("space", type=Map)["attempt"] = "failed"
    external_update = external.get_update(before)
    original = store._local_meta_exception_allowed
    calls = 0

    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected validation failure")
        return original(**kwargs)

    monkeypatch.setattr(store, "_local_meta_exception_allowed", fail_once)
    with pytest.raises(RuntimeError, match="injected validation failure"):
        store.commit(
            "meta",
            lambda doc: doc.apply_update(external_update),
            author="user:owner",
            actor=None,
        )
    retry = store.commit(
        "meta",
        lambda doc: doc["space"].__setitem__("retry", "ok"),
        author="user:owner",
        actor=None,
    )
    assert json.loads(retry.envelope_bytes)["docId"] == "meta"
    assert store._docs["meta"].get("space", type=Map).get("retry") == "ok"


def test_all_document_client_ids_have_writer_rows_after_restart_sequence(
    tmp_path, request
):
    """review-p1-r2 D.2: file-first restart then invite preserves client attribution."""

    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    state = tmp_path / "d2-state"
    first = OrgFsRuntime(state, node_id="node-owner", author="user:owner")
    space = first.facade.create_space("team")
    first.facade.write_text(space.space_id, "note.md", "one")
    first.close()

    restarted = OrgFsRuntime(state, node_id="node-owner", author="user:owner")
    request.addfinalizer(restarted.close)
    restarted.facade.write_text(space.space_id, "note.md", "two")
    restarted.facade.invite(space.space_id, "user:b")
    assert_client_ids_registered(restarted.stores.get(space.space_id))


def test_import_sql_failure_leaves_live_document_unchanged(tmp_path):
    """review-p1-r2 R3: import journals before mutating the live CRDT document."""

    source = _store(tmp_path, "r3-source", "node-a")
    _genesis(source)
    change = source.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("r3", Text("value")),
        author="user:owner",
        actor="agent:a",
    )
    target = _store(tmp_path, "r3-target", "node-b")
    for row in source._db.execute(
        "SELECT envelope_bytes FROM commits WHERE doc_id='meta' ORDER BY rowid"
    ):
        target.import_envelope(bytes(row[0]), supplier="node-a")
    before = target._doc(TREE_DOC_ID).get_update()
    target._db.set_authorizer(
        lambda action, table, _column, _database, _source: (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_INSERT and table == "commits"
            else sqlite3.SQLITE_OK
        )
    )
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        target.import_envelope(change.envelope_bytes, supplier="node-a")
    target._db.set_authorizer(None)
    assert target._docs[TREE_DOC_ID].get_update() == before
    assert (
        target._db.execute(
            "SELECT COUNT(*) FROM commits WHERE doc_id = ?", (TREE_DOC_ID,)
        ).fetchone()[0]
        == 0
    )


def test_admission_holds_store_lock_against_concurrent_commit(tmp_path, monkeypatch):
    """review-p1-r2 R6: public admission serializes with journal commits."""

    store = _store(tmp_path, "r6", "node-a")
    _genesis(store)
    change = store.commit(
        TREE_DOC_ID,
        lambda doc: doc.__setitem__("first", Text("one")),
        author="user:owner",
        actor="agent:a",
    )
    entered = threading.Event()
    release = threading.Event()
    original = store._membership_at

    def blocked(token):
        entered.set()
        assert release.wait(2)
        return original(token)

    monkeypatch.setattr(store, "_membership_at", blocked)
    admission_thread = threading.Thread(
        target=store.admission, args=(change.envelope_bytes,), daemon=True
    )
    admission_thread.start()
    assert entered.wait(2)
    committed = threading.Event()

    def concurrent_commit():
        store.commit(
            TREE_DOC_ID,
            lambda doc: doc.__setitem__("second", Text("two")),
            author="user:owner",
            actor="agent:a",
        )
        committed.set()

    commit_thread = threading.Thread(target=concurrent_commit, daemon=True)
    commit_thread.start()
    assert not committed.wait(0.1)
    release.set()
    admission_thread.join(2)
    commit_thread.join(2)
    assert committed.is_set()
