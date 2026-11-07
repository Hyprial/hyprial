"""Focused resident replica engine tests."""

from __future__ import annotations

import hashlib
import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.storage.replica import (
    MemoryReplicaBackend,
    PutOutcome,
    ReplicaStore,
    decode_snapshot_frontier,
    encode_snapshot_frontier)


class _View:
    def __init__(self) -> None:
        self.retirements: dict[str, dict[str, str]] = {}
        self.purged: set[str] = set()
        self.points: dict[str, bytes] = {}

    def retired(self, doc_id: str) -> dict[str, str] | None:
        return self.retirements.get(doc_id)

    def purge_listed(self, digest: str) -> bool:
        return digest in self.purged

    def snapshot_point(self, doc_id: str) -> bytes | None:
        return self.points.get(doc_id)


def _replica(*, retain: int = 0) -> tuple[ReplicaStore, MemoryReplicaBackend, _View]:
    backend = MemoryReplicaBackend()
    view = _View()
    return (
        ReplicaStore("space", backend, view, retain_before_snapshot=retain),
        backend,
        view,
    )


def test_replica_rejects_retired_docid_with_snapshot_barrier() -> None:
    replica, backend, view = _replica()
    view.retirements["doc-old"] = {
        "replacementDocId": "doc-new",
        "planId": "plan-a",
    }
    key = "log/space/doc-old/writer-a/0"

    with pytest.raises(OrgFsError) as stored:
        replica.store_envelope(key, b"old update")
    with pytest.raises(OrgFsError) as served:
        replica.serve_log_range("doc-old", "writer-a")

    assert stored.value.code == served.value.code == "snapshot-barrier"
    assert stored.value.details == {
        "retiredDocId": "doc-old",
        "replacementDocId": "doc-new",
        "planId": "plan-a",
    }
    assert backend.get(key) is None


def test_replica_serves_stored_bytes_verbatim() -> None:
    replica, _backend, _view = _replica()
    envelopes = (b"\x00raw-envelope", b"{not re-encoded}")
    for sequence, envelope in enumerate(envelopes):
        assert (
            replica.store_envelope(f"log/space/doc-a/writer-a/{sequence}", envelope)
            is PutOutcome.CREATED
        )

    assert (
        tuple(value for _key, value in replica.serve_log_range("doc-a", "writer-a"))
        == envelopes
    )
    assert (
        tuple(value for _key, value in replica.serve_sync_page(["doc-a"])) == envelopes
    )


def test_retired_doc_residue_only_blocks_that_doc_not_whole_sync_page() -> None:
    replica, backend, view = _replica()
    old_key = "log/space/doc-old/writer-a/0"
    live_key = "log/space/doc-live/writer-a/0"
    backend.put(old_key, b"retired residue")
    backend.put(live_key, b"live envelope")
    view.retirements["doc-old"] = {
        "replacementDocId": "doc-new",
        "planId": "plan-a",
    }

    with pytest.raises(OrgFsError) as retired_only:
        replica.serve_sync_page(["doc-old"])

    assert retired_only.value.code == "snapshot-barrier"
    assert replica.serve_sync_page() == ((live_key, b"live envelope"),)
    assert replica.serve_sync_page(["doc-old", "doc-live"]) == (
        (live_key, b"live envelope"),
    )


def test_replica_purge_duty_deletes_listed_objects_and_scan_clean() -> None:
    replica, backend, _view = _replica()
    update_blob = hashlib.sha256(b"inline overflow update").hexdigest()
    target_blob = hashlib.sha256(b"purged file bytes").hexdigest()
    owner_shared_blob = hashlib.sha256(b"other space still references").hexdigest()
    old_snapshot = b"old snapshot with sensitive bytes"
    old_snapshot_id = hashlib.sha256(old_snapshot).hexdigest()
    replacement = b"sanitized replacement snapshot"
    replacement_id = hashlib.sha256(replacement).hexdigest()

    backend.put("log/space/doc-old/writer-a/0", b"sensitive update")
    backend.put(f"snapshot/space/doc-old/{old_snapshot_id}", old_snapshot)
    replica.store_blob(update_blob, b"inline overflow update")
    replica.store_blob(target_blob, b"purged file bytes")
    replica.store_blob(owner_shared_blob, b"other space still references")
    backend.put("log/space/doc-unrelated/writer-a/0", b"keep")

    record = {
        "oldPhysicalDocId": "doc-old",
        "replacementDocId": "doc-new",
        "snapshotId": replacement_id,
        "planId": "plan-a",
    }
    plan = {
        "replacementSnapshotBytes": replacement,
        "docs": [
            {
                "docId": "doc-old",
                "from": "v0",
                "to": "v1",
                "updateBlobs": [update_blob],
            }
        ],
        "snapshots": [{"docId": "doc-old", "snapshotId": old_snapshot_id}],
        "blobs": [
            {"sha": target_blob, "referencedElsewhereLocally": []},
            {
                "sha": owner_shared_blob,
                "referencedElsewhereLocally": ["space-b"],
            },
        ],
    }

    deleted = replica.apply_retirement(record, plan)

    assert "log/space/doc-old/writer-a/0" in deleted
    assert f"snapshot/space/doc-old/{old_snapshot_id}" in deleted
    assert f"blob/{update_blob}" in deleted
    assert f"blob/{target_blob}" in deleted
    assert not tuple(backend.keys("log/space/doc-old/"))
    assert not tuple(backend.keys("snapshot/space/doc-old/"))
    assert backend.get(f"blob/{update_blob}") is None
    assert backend.get(f"blob/{target_blob}") is None
    assert backend.get(f"blob/{owner_shared_blob}") is None
    assert backend.get("log/space/doc-unrelated/writer-a/0") == b"keep"
    assert backend.get(f"snapshot/space/doc-new/{replacement_id}") == replacement


def test_replica_retention_drops_pre_snapshot_point_keys() -> None:
    replica, backend, view = _replica(retain=0)
    for sequence in range(6):
        backend.put(
            f"log/space/doc-a/writer-a/{sequence}",
            f"envelope-{sequence}".encode(),
        )
    backend.put("log/space/doc-a/writer-b/0", b"uncovered writer")
    view.points["doc-a"] = encode_snapshot_frontier({"writer-a": 3})

    deleted = replica.apply_retention("doc-a")

    assert deleted == tuple(
        f"log/space/doc-a/writer-a/{sequence}" for sequence in range(4)
    )
    assert tuple(backend.keys("log/space/doc-a/")) == (
        "log/space/doc-a/writer-a/4",
        "log/space/doc-a/writer-a/5",
        "log/space/doc-a/writer-b/0",
    )


def test_m8_sync_page_after_key_uses_numeric_sequence_order() -> None:
    replica, _backend, _view = _replica()
    for sequence in (2, 10, 11):
        replica.store_envelope(
            f"log/space/doc-a/writer-a/{sequence}",
            f"envelope-{sequence}".encode(),
        )

    page = replica.serve_sync_page(
        ["doc-a"],
        after_key="log/space/doc-a/writer-a/2",
        limit=10,
    )

    assert tuple(key for key, _value in page) == (
        "log/space/doc-a/writer-a/10",
        "log/space/doc-a/writer-a/11",
    )


def test_snapshot_frontier_encoding_round_trips_store_watermarks() -> None:
    encoded = encode_snapshot_frontier({"writer-b": 7, "writer-a": 3})

    assert encoded == (b'{"schemaVersion":1,"writers":{"writer-a":3,"writer-b":7}}')
    assert decode_snapshot_frontier(encoded) == {"writer-a": 3, "writer-b": 7}
    with pytest.raises(ValueError, match="snapshot frontier schema"):
        decode_snapshot_frontier(b'{"writer-a":3}')


def test_replica_purged_blob_answers_purged() -> None:
    replica, backend, view = _replica()
    content = b"same forbidden bytes"
    digest = hashlib.sha256(content).hexdigest()
    backend.put(f"blob/{digest}", content)
    view.purged.add(digest)

    with pytest.raises(OrgFsError) as stored:
        replica.store_blob(digest, content)
    with pytest.raises(OrgFsError) as served:
        replica.serve_blob_chunk(digest)

    assert stored.value.code == served.value.code == "purged"


def test_replica_records_all_stored_blobs_as_pinned() -> None:
    backend = MemoryReplicaBackend()
    view = _View()
    content = b"resident replica blob"
    digest = hashlib.sha256(content).hexdigest()
    backend.put(f"blob/{digest}", content)
    replica = ReplicaStore("space", backend, view)

    replica.store_blob(digest, content)

    assert replica.pinned_reason(digest) == "replica"
    assert replica.pinned_blobs() == (digest,)


def test_reconcile_blobs_reads_only_digest_added_by_second_commit() -> None:
    replica, backend, _view = _replica()

    class _CommittedBlobs:
        def __init__(self) -> None:
            self.contents: dict[str, bytes] = {}
            self.get_calls: list[str] = []

        def commit(self, content: bytes) -> str:
            digest = hashlib.sha256(content).hexdigest()
            self.contents[digest] = content
            return digest

        def references(self, space_id: str):
            return tuple(
                (space_id, digest, "own-write") for digest in self.contents
            )

        def get(self, _space_id: str, digest: str) -> bytes:
            self.get_calls.append(digest)
            return self.contents[digest]

    blobs = _CommittedBlobs()
    first = blobs.commit(b"first commit")
    assert replica.reconcile_blobs(blobs) == (first,)
    assert blobs.get_calls == [first]

    blobs.get_calls.clear()
    second = blobs.commit(b"second commit")
    assert replica.reconcile_blobs(blobs) == (second,)

    assert blobs.get_calls == [second]
    assert backend.exists(f"blob/{first}")
    assert backend.exists(f"blob/{second}")
