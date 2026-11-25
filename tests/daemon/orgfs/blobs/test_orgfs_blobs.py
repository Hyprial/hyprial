"""Blob-store tests through the public content-addressed API."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.storage.blobs import (
    BlobIntegrityError,
    BlobMissing,
    BlobStore)


def test_put_is_content_addressed_and_refs_are_scoped_by_space(tmp_path: Path) -> None:
    data = b"same bytes in two spaces"
    with BlobStore(tmp_path) as blobs:
        digest_a = blobs.put("space-a", data, reason="own-write")
        digest_b = blobs.put("space-b", data, reason="own-write")
        blobs.put("space-a", data, reason="own-write")
        blobs.pin("space-a", digest_a, "replica")

        assert digest_a == digest_b == hashlib.sha256(data).hexdigest()
        assert blobs.get("space-a", digest_a) == data
        assert blobs.get("space-b", digest_b) == data
        assert blobs.references(digest=digest_a) == (
            ("space-a", digest_a, "own-write"),
            ("space-a", digest_a, "replica"),
            ("space-b", digest_b, "own-write"),
        )
        path = tmp_path / "orgfs" / "blobs" / digest_a[:2] / digest_a
        assert path.is_file()
        assert path.read_bytes() == data

        with sqlite3.connect(tmp_path / "orgfs" / "blob-refs.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM blob_refs").fetchone() == (3,)


def test_tampering_is_rejected_and_missing_digest_is_explicit(tmp_path: Path) -> None:
    with BlobStore(tmp_path) as blobs:
        digest = blobs.put("space", b"original", reason="own-write")
        path = tmp_path / "orgfs" / "blobs" / digest[:2] / digest
        path.write_bytes(b"tampered")

        assert not blobs.has(digest)
        with pytest.raises(BlobIntegrityError):
            blobs.get("space", digest)
        with pytest.raises(BlobMissing) as missing:
            blobs.get("space", "0" * 64)
        assert missing.value.code == "unknown-blob"


def test_review_fix_14_integrity_failure_uses_frozen_wire_code(tmp_path: Path) -> None:
    """Review P1 fix 14: blob integrity failures map to frozen unknown-blob."""

    with BlobStore(tmp_path) as blobs:
        digest = blobs.put("space", b"original", reason="own-write")
        path = tmp_path / "orgfs" / "blobs" / digest[:2] / digest
        path.write_bytes(b"tampered")

        response = json.loads(
            blobs.handle_request(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-blob",
                    "spaceId": "space",
                    "digest": digest,
                    "requester": {"node": "node-a", "author": "user:a"},
                }
            )
        )
        assert response["code"] == "unknown-blob"
        assert response["details"] == {"digest": digest}


def _unpin_fixture(blobs) -> str:
    digest = blobs.put("space-a", b"shared by reasons", reason="sync")
    blobs.pin("space-a", digest, "own-write")
    blobs.pin("space-b", digest, "sync")
    return digest


def test_unpin_releases_only_one_reason_of_one_space(tmp_path: Path) -> None:
    with BlobStore(tmp_path) as blobs:
        digest = _unpin_fixture(blobs)
        blobs.unpin("space-a", digest, "sync")
        assert blobs.references(digest=digest) == (
            ("space-a", digest, "own-write"),
            ("space-b", digest, "sync"),
        )
        assert blobs.delete_if_unreferenced(digest) is False
        blobs.unpin("space-a", digest, "sync")
        blobs.unpin("space-a", digest, "own-write")
        blobs.unpin("space-b", digest, "sync")
        assert blobs.references(digest=digest) == ()
        assert blobs.delete_if_unreferenced(digest) is True


def test_unpin_rejects_a_malformed_digest_or_reason(tmp_path: Path) -> None:
    with BlobStore(tmp_path) as blobs:
        digest = _unpin_fixture(blobs)
        with pytest.raises(ValueError):
            blobs.unpin("space-a", "not-a-digest", "sync")
        with pytest.raises(ValueError):
            blobs.unpin("space-a", digest, "")
        assert len(blobs.references(digest=digest)) == 3


def test_release_still_drops_every_reason_of_the_space(tmp_path: Path) -> None:
    with BlobStore(tmp_path) as blobs:
        digest = _unpin_fixture(blobs)
        blobs.release("space-a", digest)
        assert blobs.references(digest=digest) == (("space-b", digest, "sync"),)


def test_the_blob_authority_forwards_unpin(tmp_path: Path) -> None:
    from hyprial.daemon.impl.orgfs.storage.blob_authority import BlobAuthority

    blobs = BlobAuthority(tmp_path)
    try:
        digest = _unpin_fixture(blobs)
        blobs.unpin("space-a", digest, "sync")
        assert blobs.references(digest=digest) == (
            ("space-a", digest, "own-write"),
            ("space-b", digest, "sync"),
        )
    finally:
        blobs.close()
