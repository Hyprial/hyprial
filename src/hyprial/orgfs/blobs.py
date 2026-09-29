"""Content-addressed storage and wire helpers for the orgfs blob plane.

The blob plane deliberately has no transport dependency.  ``BlobStore`` owns
the immutable bytes and the per-space reference ledger; the codec functions
produce and consume the payloads used by the transport slice.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable, Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Any, Final, TypeAlias


ORGFS_BLOB_CHUNK_BYTES: Final[int] = 768_000
ORGFS_PAGE_BYTES: Final[int] = 1_048_576
_SCHEMA_VERSION: Final[int] = 1
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")

BlobPayload: TypeAlias = bytes | bytearray | memoryview
ChunkInput: TypeAlias = bytes | bytearray | memoryview | Mapping[str, Any]


class BlobError(Exception):
    """Base error with the corresponding orgfs wire error code."""

    code: str = "blob-error"

    def __init__(
        self, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class BlobMissing(FileNotFoundError, BlobError):
    """The requested digest is not present on this node."""

    code = "unknown-blob"

    def __init__(self, digest: str) -> None:
        BlobError.__init__(self, f"unknown blob: {digest}", details={"digest": digest})
        self.digest = digest


class BlobPurged(BlobError):
    """The requested digest is on the purge-list source."""

    code = "purged"

    def __init__(self, digest: str) -> None:
        super().__init__(f"blob is purged: {digest}", details={"digest": digest})
        self.digest = digest


class BlobRangeError(BlobError, ValueError):
    """A chunk request is not aligned or lies outside the blob."""

    code = "out-of-range"


class BlobIntegrityError(BlobError, ValueError):
    """Stored or assembled bytes do not match their content address."""

    # A corrupt object is not a valid instance of its requested content address.
    # ``unknown-blob`` is the frozen wire code that lets callers try another
    # holder without misclassifying a valid request as invalid.
    code = "unknown-blob"


def _state_dir() -> Path:
    """Return the repository's configured state root.

    Importing the selector lazily avoids importing the CLI for callers that
    provide an explicit root, while ensuring the default follows the same
    ``HARNESS_STATE_DIR``/``HYPRIAL_HOME`` rules as production code.
    """

    from hyprial.cli import _state_dir as select_state_dir

    return select_state_dir()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _mapping(
    payload: bytes | bytearray | memoryview | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    try:
        decoded = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("blob payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("blob payload must be a JSON object")
    return decoded


def _require_digest(digest: str) -> str:
    if not isinstance(digest, str) or _HEX_DIGEST.fullmatch(digest) is None:
        raise ValueError("digest must be a lowercase SHA-256 hex string")
    return digest


def _request_values(
    request: bytes | Mapping[str, Any],
) -> tuple[dict[str, Any], int, int]:
    value = _mapping(request)
    if (
        value.get("schemaVersion") != _SCHEMA_VERSION
        or value.get("type") != "orgfs-blob"
    ):
        raise ValueError("invalid orgfs-blob request")
    if not isinstance(value.get("spaceId"), str) or not value["spaceId"]:
        raise ValueError("orgfs-blob request requires spaceId")
    _require_digest(value.get("digest"))
    offset = value.get("offset", 0)
    length = value.get("length", ORGFS_BLOB_CHUNK_BYTES)
    if type(offset) is not int or type(length) is not int:
        raise BlobRangeError("chunk offset and length must be integers")
    if offset < 0 or offset % ORGFS_BLOB_CHUNK_BYTES:
        raise BlobRangeError("chunk offset must be a non-negative chunk boundary")
    if length <= 0 or length > ORGFS_BLOB_CHUNK_BYTES:
        raise BlobRangeError("chunk length is outside the allowed range")
    return value, offset, length


def encode_request(
    space_id: str,
    digest: str,
    offset: int = 0,
    length: int | None = None,
) -> bytes:
    """Encode an ``orgfs-blob`` request as canonical UTF-8 JSON bytes."""

    if not isinstance(space_id, str) or not space_id:
        raise ValueError("space_id must be a non-empty string")
    digest = _require_digest(digest)
    request: dict[str, Any] = {
        "schemaVersion": _SCHEMA_VERSION,
        "type": "orgfs-blob",
        "spaceId": space_id,
        "digest": digest,
    }
    if offset != 0:
        request["offset"] = offset
    if length is not None:
        request["length"] = length
    # Validate omitted defaults as well as explicit values.
    _request_values(request)
    return _json_bytes(request)


def decode_request(request: bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Decode and validate a blob request, applying protocol defaults."""

    value, offset, length = _request_values(request)
    value["offset"] = offset
    value["length"] = length
    return value


def validate_request(request: bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Public alias for request validation used by query handlers."""

    return decode_request(request)


def validate(request: bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Short frozen-name alias for the request validator."""

    return validate_request(request)


def encode_chunk(
    digest: str,
    offset: int,
    total: int,
    data: BlobPayload,
) -> bytes:
    """Encode one chunk and enforce the complete encoded 1 MiB page bound."""

    digest = _require_digest(digest)
    raw = bytes(data)
    if type(offset) is not int or type(total) is not int:
        raise BlobRangeError("chunk offset and total must be integers")
    if total < 0 or offset < 0 or offset % ORGFS_BLOB_CHUNK_BYTES:
        raise BlobRangeError("chunk offset or total is invalid")
    if len(raw) > ORGFS_BLOB_CHUNK_BYTES or offset + len(raw) > total:
        raise BlobRangeError("chunk lies outside the blob")
    if not raw and total:
        raise BlobRangeError("non-empty blob chunk must make progress")
    if total and offset >= total:
        raise BlobRangeError("chunk lies outside the blob")
    value = {
        "schemaVersion": _SCHEMA_VERSION,
        "type": "orgfs-blob-chunk",
        "digest": digest,
        "offset": offset,
        "length": len(raw),
        "total": total,
        "data": base64.b64encode(raw).decode("ascii"),
    }
    encoded = _json_bytes(value)
    if len(encoded) > ORGFS_PAGE_BYTES:
        raise ValueError("encoded blob chunk exceeds ORGFS_PAGE_BYTES")
    return encoded


def decode_chunk(chunk: ChunkInput) -> dict[str, Any]:
    """Decode a chunk and validate its shape, bounds, and base64 data."""

    value = _mapping(chunk)
    if (
        value.get("schemaVersion") != _SCHEMA_VERSION
        or value.get("type") != "orgfs-blob-chunk"
    ):
        raise ValueError("invalid orgfs-blob-chunk response")
    digest = _require_digest(value.get("digest"))
    offset = value.get("offset")
    length = value.get("length")
    total = value.get("total")
    if any(type(item) is not int for item in (offset, length, total)):
        raise BlobRangeError("chunk numeric fields must be integers")
    if offset < 0 or offset % ORGFS_BLOB_CHUNK_BYTES or length < 0:
        raise BlobRangeError("chunk offset or length is invalid")
    if length > ORGFS_BLOB_CHUNK_BYTES or total < 0 or offset + length > total:
        raise BlobRangeError("chunk lies outside the blob")
    if length == 0 and total:
        raise BlobRangeError("non-empty blob chunk must make progress")
    if total and offset >= total:
        raise BlobRangeError("chunk lies outside the blob")
    encoded_data = value.get("data")
    if not isinstance(encoded_data, str):
        raise ValueError("chunk data must be base64 text")
    try:
        raw = base64.b64decode(encoded_data.encode("ascii"), validate=True)
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("chunk data is not valid base64") from exc
    if len(raw) != length:
        raise BlobRangeError("chunk length does not match decoded data")
    value["digest"] = digest
    value["dataBytes"] = raw
    return value


def assemble(
    digest: str,
    chunks: Iterable[ChunkInput],
) -> bytes:
    """Assemble chunks in any order and verify the final content address."""

    digest = _require_digest(digest)
    decoded = [decode_chunk(chunk) for chunk in chunks]
    if not decoded:
        raise BlobIntegrityError("no chunks supplied", details={"digest": digest})
    total = decoded[0]["total"]
    if any(item["digest"] != digest or item["total"] != total for item in decoded):
        raise BlobIntegrityError(
            "chunk set has inconsistent digest or total", details={"digest": digest}
        )
    ordered = sorted(decoded, key=lambda item: item["offset"])
    cursor = 0
    parts: list[bytes] = []
    for item in ordered:
        if item["offset"] != cursor:
            raise BlobIntegrityError(
                "chunk set has a gap or overlap", details={"digest": digest}
            )
        parts.append(item["dataBytes"])
        cursor += item["length"]
    if cursor != total:
        raise BlobIntegrityError("chunk set is incomplete", details={"digest": digest})
    result = b"".join(parts)
    if hashlib.sha256(result).hexdigest() != digest:
        raise BlobIntegrityError(
            "assembled blob hash does not match digest", details={"digest": digest}
        )
    return result


def error_payload(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the frozen orgfs error shape."""

    return {
        "schemaVersion": _SCHEMA_VERSION,
        "type": "orgfs-error",
        "code": code,
        "message": message,
        "details": dict(details or {}),
    }


def encode_error(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> bytes:
    return _json_bytes(error_payload(code, message, details))


class BlobStore:
    """File-backed content-addressed blob store with a space ref ledger."""

    def __init__(
        self,
        state_root: Path | str | None = None,
        *,
        purge_checker: Callable[[str, str], bool] | None = None,
    ) -> None:
        root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None
            else _state_dir()
        )
        self.state_root = root
        self.blob_root = root / "orgfs" / "blobs"
        self.refs_path = root / "orgfs" / "blob-refs.sqlite3"
        self._purge_checker = purge_checker
        self._space_purge_projections: dict[str, frozenset[str]] = {}
        self._purge_checker_lock = threading.Lock()
        self.blob_root.mkdir(parents=True, exist_ok=True)
        # pysqlite connections are not safe for concurrent execute/close;
        # zenoh callback threads share this store with caller threads.
        self._db_lock = threading.RLock()
        self._db = sqlite3.connect(self.refs_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS blob_refs (
                spaceId TEXT NOT NULL,
                sha TEXT NOT NULL,
                pinnedReason TEXT NOT NULL,
                PRIMARY KEY (spaceId, sha, pinnedReason)
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        with self._db_lock:
            if getattr(self, "_db", None) is not None:
                self._db.close()
                self._db = None  # type: ignore[assignment]

    def __enter__(self) -> BlobStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _path(self, digest: str) -> Path:
        digest = _require_digest(digest)
        return self.blob_root / digest[:2] / digest

    def _check_purged(self, space_id: str, digest: str) -> None:
        with self._purge_checker_lock:
            projection = self._space_purge_projections.get(space_id, frozenset())
            global_checker = self._purge_checker
        if digest in projection or (
            global_checker is not None and global_checker(space_id, digest)
        ):
            raise BlobPurged(digest)

    def register_purge_checker(
        self, space_id: str, checker: Callable[[str], bool]
    ) -> None:
        """Compatibility seam for legacy injected purge projections."""

        with self._purge_checker_lock:
            self._space_purge_projections[str(space_id)] = frozenset(
                digest
                for digest in (path.name for path in self.blob_root.glob("*/*"))
                if checker(digest)
            )

    def register_purge_projection(
        self, space_id: str, digests: Iterable[str]
    ) -> None:
        """Publish an immutable per-space purge set without a callback edge."""

        projection = frozenset(_require_digest(digest) for digest in digests)
        with self._purge_checker_lock:
            self._space_purge_projections[str(space_id)] = projection

    def put(self, space_id: str, data: BlobPayload, *, reason: str) -> str:
        """Store bytes once and add one idempotent per-space reference."""

        if not isinstance(space_id, str) or not space_id:
            raise ValueError("space_id must be a non-empty string")
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        raw = bytes(data)
        digest = hashlib.sha256(raw).hexdigest()
        self._check_purged(space_id, digest)
        path = self._path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    # Another writer won the content-addressed race.  Its
                    # immutable bytes are the canonical copy.
                    pass
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        self.pin(space_id, digest, reason)
        return digest

    def get(self, space_id: str, digest: str) -> bytes:
        digest = _require_digest(digest)
        self._check_purged(space_id, digest)
        path = self._path(digest)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobMissing(digest) from exc
        if hashlib.sha256(raw).hexdigest() != digest:
            raise BlobIntegrityError(
                "stored blob hash does not match digest", details={"digest": digest}
            )
        return raw

    def has(self, digest: str) -> bool:
        try:
            path = self._path(digest)
        except ValueError:
            return False
        if not path.is_file():
            return False
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() == digest
        except OSError:
            return False

    def contains(self, digest: str) -> bool:
        """Return whether the content-addressed path exists without reading it."""

        try:
            return self._path(digest).is_file()
        except (OSError, ValueError):
            return False

    def pin(self, space_id: str, digest: str, reason: str) -> None:
        if not isinstance(space_id, str) or not space_id:
            raise ValueError("space_id must be a non-empty string")
        digest = _require_digest(digest)
        self._check_purged(space_id, digest)
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        if not self._path(digest).is_file():
            raise BlobMissing(digest)
        with self._db_lock:
            self._db.execute(
                "INSERT OR IGNORE INTO blob_refs(spaceId, sha, pinnedReason) VALUES (?, ?, ?)",
                (space_id, digest, reason),
            )
            self._db.commit()

    def references(
        self,
        space_id: str | None = None,
        digest: str | None = None,
    ) -> tuple[tuple[str, str, str], ...]:
        """Return refs for diagnostics and tests without exposing the DB."""

        query = "SELECT spaceId, sha, pinnedReason FROM blob_refs"
        conditions: list[str] = []
        values: list[str] = []
        if space_id is not None:
            conditions.append("spaceId = ?")
            values.append(space_id)
        if digest is not None:
            conditions.append("sha = ?")
            values.append(_require_digest(digest))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY spaceId, sha, pinnedReason"
        with self._db_lock:
            return tuple(self._db.execute(query, values).fetchall())

    def referenced_elsewhere(self, space_id: str, digest: str) -> tuple[str, ...]:
        """Return other local spaces that retain this content-addressed object."""

        digest = _require_digest(digest)
        with self._db_lock:
            return tuple(
                str(row[0])
                for row in self._db.execute(
                    "SELECT DISTINCT spaceId FROM blob_refs "
                    "WHERE sha = ? AND spaceId <> ? ORDER BY spaceId",
                    (digest, space_id),
                )
            )

    def release(self, space_id: str, digest: str) -> None:
        """Release every reason held by one space without touching other spaces."""

        digest = _require_digest(digest)
        with self._db_lock, self._db:
            self._db.execute(
                "DELETE FROM blob_refs WHERE spaceId = ? AND sha = ?",
                (space_id, digest),
            )

    def delete_if_unreferenced(self, digest: str) -> bool:
        """Delete immutable bytes only when no local space still references them."""

        digest = _require_digest(digest)
        with self._db_lock:
            if (
                self._db.execute(
                    "SELECT 1 FROM blob_refs WHERE sha = ? LIMIT 1", (digest,)
                ).fetchone()
                is not None
            ):
                return False
        try:
            self._path(digest).unlink()
        except FileNotFoundError:
            pass
        return True

    def get_chunk(
        self,
        request: bytes | Mapping[str, Any],
    ) -> bytes:
        """Serve a validated request as one encoded chunk or raise BlobError."""

        value = decode_request(request)
        space_id = value["spaceId"]
        digest = value["digest"]
        offset = value["offset"]
        length = value["length"]
        raw = self.get(space_id, digest)
        if offset > len(raw):
            raise BlobRangeError("chunk request lies outside the blob")
        # The default request asks for the wire maximum because a caller may
        # only know the digest.  A holder must return the shorter final chunk
        # instead of requiring out-of-band size metadata.
        length = min(length, len(raw) - offset)
        return encode_chunk(digest, offset, len(raw), raw[offset : offset + length])

    def chunk(self, request: bytes | Mapping[str, Any]) -> bytes:
        """Short alias used by simple query handlers."""

        return self.get_chunk(request)

    def handle_request(self, request: bytes | Mapping[str, Any]) -> bytes:
        """Return either a chunk or the frozen error response shape."""

        try:
            return self.get_chunk(request)
        except BlobError as exc:
            return encode_error(exc.code, str(exc), exc.details)
        except (TypeError, ValueError) as exc:
            return encode_error("invalid-argument", str(exc))


FileBlobStore = BlobStore


__all__ = [
    "BlobError",
    "BlobIntegrityError",
    "BlobMissing",
    "BlobPurged",
    "BlobRangeError",
    "BlobStore",
    "FileBlobStore",
    "ORGFS_BLOB_CHUNK_BYTES",
    "ORGFS_PAGE_BYTES",
    "assemble",
    "decode_chunk",
    "decode_request",
    "encode_chunk",
    "encode_error",
    "encode_request",
    "error_payload",
    "validate",
    "validate_request",
]
