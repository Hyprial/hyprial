# Decisions:
# - Migrated from the user-visible half of contracts/test_orgfs_api.py
#   ``test_members_status_blobs_watch_and_store_commit`` (blob read/write and
#   the ``*.bin`` watch); the store-commit and status halves are journal
#   specific and not migrated (triage §3.5).
# - Durable variants run on make_runtime so the blob store is the real one.
"""Binary write_bytes / read_bytes and blob contracts."""

from __future__ import annotations

import hashlib

import pytest


def test_bytes_round_trip_with_content_addressed_hash(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    payload = bytes(range(256)) * 4

    info = fs.write_bytes(space.space_id, "data.bin", payload)

    assert info.kind == "blob"
    assert info.size == len(payload)
    assert info.blob_hash == hashlib.sha256(payload).hexdigest()
    assert info.doc_id is None
    assert fs.read_bytes(space.space_id, "data.bin") == payload
    assert fs.stat(space.space_id, f"id:{info.node_id}").blob_hash == info.blob_hash


def test_bytes_overwrite_keeps_the_node(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.write_bytes(space.space_id, "data.bin", b"binary")

    second = fs.write_bytes(space.space_id, f"id:{first.node_id}", b"changed")

    assert second.node_id == first.node_id
    assert second.blob_hash != first.blob_hash
    assert second.version != first.version
    assert fs.read_bytes(space.space_id, "data.bin") == b"changed"


def test_read_bytes_of_a_text_document_returns_its_utf8(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.write_text(space.space_id, "notes.md", "text \u00e9")

    assert fs.read_bytes(space.space_id, "notes.md") == "text \u00e9".encode()


def test_bytes_watch_reports_created_until_closed(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    seen = []
    watcher = fs.watch(space.space_id, "*.bin", seen.append)

    fs.write_bytes(space.space_id, "data.bin", b"binary")
    fs.write_text(space.space_id, "notes.md", "ignored by the glob")
    _wait_for(lambda: len(seen) == 1)
    watcher.close()
    fs.write_bytes(space.space_id, "data.bin", b"changed")

    assert [(event.kind, event.node.path) for event in seen] == [("created", "data.bin")]


def test_bytes_survive_reopen(orgfs_impl, tmp_path) -> None:
    node = orgfs_impl.make_runtime(tmp_path / "node", "node-a", "user:owner")
    space = node.facade.create_space("team")
    payload = b"\x00binary\xff" * 1000
    info = node.facade.write_bytes(space.space_id, "data.bin", payload)

    reopened = node.reopen()

    assert reopened.read_bytes(space.space_id, "data.bin") == payload
    assert reopened.stat(space.space_id, "data.bin").blob_hash == info.blob_hash


@pytest.mark.parametrize("size", [0, 1, 4 * 1024 * 1024 + 1])
def test_bytes_sizes_round_trip(orgfs_impl, size: int) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    payload = b"z" * size

    info = fs.write_bytes(space.space_id, "sized.bin", payload)

    assert info.size == size
    assert fs.read_bytes(space.space_id, "sized.bin") == payload


def _wait_for(predicate, timeout: float = 2.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)
