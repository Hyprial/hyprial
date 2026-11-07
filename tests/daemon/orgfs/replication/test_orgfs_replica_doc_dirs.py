"""Replica document directories are named by a hash of the id."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.orgfs.storage.replica import FsReplicaBackend, PutOutcome
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import document_file_stem
from hyprial.identity import protected_directory_doc_id

SPACE_ID = "a2b5fb64-de25-4897-aaf1-0e25e2d83e93"
INVITE_PATH = "directory/invites/" + "0" * 64 + ".json"


def _invite_doc_id() -> str:
    doc_id = protected_directory_doc_id(
        SPACE_ID, INVITE_PATH, space_owner="user:bob.smith@example.com"
    )
    assert doc_id is not None and len(doc_id) == 253
    return doc_id


def _space_root(tmp_path: Path) -> Path:
    return tmp_path / "orgfs" / "replica" / SPACE_ID


def test_a_production_length_document_id_round_trips(tmp_path: Path) -> None:
    doc_id = _invite_doc_id()
    backend = FsReplicaBackend(SPACE_ID, tmp_path)
    log_key = f"log/{SPACE_ID}/{doc_id}/node-hq-01.example-tailnet.ts.net/0"
    snapshot_key = f"snapshot/{SPACE_ID}/{doc_id}/" + "a" * 64

    assert backend.put(log_key, b"envelope") is PutOutcome.CREATED
    assert backend.put(snapshot_key, b"snapshot") is PutOutcome.CREATED

    reopened = FsReplicaBackend(SPACE_ID, tmp_path)
    assert reopened.get(log_key) == b"envelope"
    assert tuple(reopened.keys(f"log/{SPACE_ID}/")) == (log_key,)
    assert tuple(reopened.keys(f"snapshot/{SPACE_ID}/{doc_id}/")) == (snapshot_key,)
    stem = document_file_stem(doc_id)
    assert (_space_root(tmp_path) / "log" / SPACE_ID / stem / ".docid").read_text() == doc_id


def test_a_document_id_past_a_file_name_round_trips(tmp_path: Path) -> None:
    doc_id = protected_directory_doc_id(
        SPACE_ID,
        "directory/devices/amy.williams@example.com/" + "d" * 200 + ".json",
        space_owner="user:bob.smith@example.com",
    )
    assert doc_id is not None and len(doc_id) > 255
    backend = FsReplicaBackend(SPACE_ID, tmp_path)
    key = f"log/{SPACE_ID}/{doc_id}/node-hq-01.example-tailnet.ts.net/0"

    assert backend.put(key, b"envelope") is PutOutcome.CREATED
    assert FsReplicaBackend(SPACE_ID, tmp_path).get(key) == b"envelope"


def test_id_named_directories_migrate_and_keep_their_keys(tmp_path: Path) -> None:
    legacy = _space_root(tmp_path) / "log" / SPACE_ID / "doc-a" / "writer-a"
    legacy.mkdir(parents=True)
    (legacy / "0").write_bytes(b"zero")
    (legacy / "1").write_bytes(b"one")

    backend = FsReplicaBackend(SPACE_ID, tmp_path)

    assert not (_space_root(tmp_path) / "log" / SPACE_ID / "doc-a").exists()
    assert tuple(backend.keys(f"log/{SPACE_ID}/")) == (
        f"log/{SPACE_ID}/doc-a/writer-a/0",
        f"log/{SPACE_ID}/doc-a/writer-a/1",
    )
    assert backend.get(f"log/{SPACE_ID}/doc-a/writer-a/1") == b"one"


def test_with_both_directories_present_objects_merge_into_the_hashed_one(
    tmp_path: Path,
) -> None:
    backend = FsReplicaBackend(SPACE_ID, tmp_path)
    assert backend.put(f"log/{SPACE_ID}/doc-a/writer-a/1", b"one") is PutOutcome.CREATED
    legacy = _space_root(tmp_path) / "log" / SPACE_ID / "doc-a" / "writer-a"
    legacy.mkdir(parents=True)
    (legacy / "0").write_bytes(b"zero")
    (legacy / "1").write_bytes(b"one")

    reopened = FsReplicaBackend(SPACE_ID, tmp_path)

    assert not (_space_root(tmp_path) / "log" / SPACE_ID / "doc-a").exists()
    assert tuple(reopened.keys(f"log/{SPACE_ID}/doc-a/")) == (
        f"log/{SPACE_ID}/doc-a/writer-a/0",
        f"log/{SPACE_ID}/doc-a/writer-a/1",
    )
