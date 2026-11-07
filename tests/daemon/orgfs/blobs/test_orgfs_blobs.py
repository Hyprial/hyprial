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
    BlobPurged,
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
                }
            )
        )
        assert response["code"] == "unknown-blob"
        assert response["details"] == {"digest": digest}


def test_purge_checker_is_space_specific_and_does_not_delete_bytes(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"retained").hexdigest()
    with BlobStore(tmp_path, purge_checker=lambda space, value: space == "purged" and value == digest) as blobs:
        blobs.put("kept", b"retained", reason="tree-ref")
        # The P1 provider hook can reject one space while another still reads
        # the same content-addressed bytes.
        with pytest.raises(BlobPurged) as purged:
            blobs.get("purged", digest)
        assert purged.value.code == "purged"
        assert blobs.get("kept", digest) == b"retained"
        assert blobs.has(digest)
