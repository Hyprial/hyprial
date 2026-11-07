"""Pure blob wire codec and chunk-serving tests."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.storage.blobs import (
    BlobRangeError,
    BlobStore,
    ORGFS_BLOB_CHUNK_BYTES,
    ORGFS_PAGE_BYTES,
    assemble,
    decode_chunk,
    decode_request,
    encode_chunk,
    encode_request)


def test_multi_chunk_round_trip_accepts_reordered_chunks(tmp_path: Path) -> None:
    data = bytes(index % 251 for index in range(ORGFS_BLOB_CHUNK_BYTES * 2 + 123))
    with BlobStore(tmp_path) as blobs:
        digest = blobs.put("space", data, reason="own-write")
        encoded = [
            blobs.get_chunk(
                encode_request(
                    "space",
                    digest,
                    offset=offset,
                    length=min(ORGFS_BLOB_CHUNK_BYTES, len(data) - offset),
                )
            )
            for offset in range(0, len(data), ORGFS_BLOB_CHUNK_BYTES)
        ]

    assert all(len(chunk) <= ORGFS_PAGE_BYTES for chunk in encoded)
    assert assemble(digest, reversed(encoded)) == data


def test_chunk_codec_rejects_tampering_only_after_whole_blob_is_assembled() -> None:
    data = b"a" * (ORGFS_BLOB_CHUNK_BYTES + 17)
    digest = hashlib.sha256(data).hexdigest()
    first = encode_chunk(digest, 0, len(data), data[:ORGFS_BLOB_CHUNK_BYTES])
    second = json.loads(
        encode_chunk(digest, ORGFS_BLOB_CHUNK_BYTES, len(data), data[ORGFS_BLOB_CHUNK_BYTES:])
    )
    second["data"] = base64.b64encode(b"tampered").decode("ascii")
    second["length"] = len(b"tampered")

    with pytest.raises(ValueError):
        assemble(digest, [first, second])


def test_request_defaults_alignment_and_range_errors() -> None:
    digest = hashlib.sha256(b"blob").hexdigest()
    request = decode_request(encode_request("space", digest))
    assert request["offset"] == 0
    assert request["length"] == ORGFS_BLOB_CHUNK_BYTES

    with pytest.raises(BlobRangeError):
        encode_request("space", digest, offset=1)
    with pytest.raises(BlobRangeError):
        encode_request("space", digest, length=ORGFS_BLOB_CHUNK_BYTES + 1)
    with pytest.raises(BlobRangeError):
        decode_request(
            {"schemaVersion": 1, "type": "orgfs-blob", "spaceId": "space", "digest": digest, "offset": -1}
        )


def test_encoded_chunk_bound_and_error_shape(tmp_path: Path) -> None:
    data = b"x" * ORGFS_BLOB_CHUNK_BYTES
    digest = hashlib.sha256(data).hexdigest()
    encoded = encode_chunk(digest, 0, len(data), data)
    assert len(encoded) <= ORGFS_PAGE_BYTES
    assert decode_chunk(encoded)["dataBytes"] == data

    with BlobStore(tmp_path) as blobs:
        unknown = blobs.handle_request(encode_request("space", "0" * 64))
    response = json.loads(unknown)
    assert response == {
        "schemaVersion": 1,
        "type": "orgfs-error",
        "code": "unknown-blob",
        "message": "unknown blob: " + "0" * 64,
        "details": {"digest": "0" * 64},
    }


def test_default_request_returns_short_final_chunk_without_size_hint(
    tmp_path: Path,
) -> None:
    data = b"size is not present in the tree entry"
    with BlobStore(tmp_path) as blobs:
        digest = blobs.put("space", data, reason="own-write")
        chunk = blobs.get_chunk(encode_request("space", digest))

    assert decode_chunk(chunk)["dataBytes"] == data
    assert assemble(digest, [chunk]) == data


def test_round3_r5_rejects_a_nonterminal_zero_length_chunk() -> None:
    """R5: a holder cannot make a non-empty fetch loop without progress."""

    digest = hashlib.sha256(b"data").hexdigest()
    chunk = {
        "schemaVersion": 1,
        "type": "orgfs-blob-chunk",
        "digest": digest,
        "offset": 0,
        "length": 0,
        "total": 4,
        "data": "",
    }

    with pytest.raises(BlobRangeError, match="make progress"):
        decode_chunk(chunk)
