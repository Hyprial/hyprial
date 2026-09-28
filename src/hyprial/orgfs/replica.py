"""Resident replica backends and byte-preserving storage duties.

This module deliberately has no transport or CRDT-store dependency.  A
resident replica stores immutable objects under the P2 key layout and exposes
raw bytes to the mesh integration layer.  Retirement and purge state are read
through an injected view so the engine cannot accidentally apply an old
physical document after a rebuild.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Final, Protocol

from hyprial.home import configured_hyprial_home

from .api import OrgFsError


ORGFS_REPLICA_RETAIN_BEFORE_SNAPSHOT: Final[int] = 0
SNAPSHOT_FRONTIER_SCHEMA_VERSION: Final[int] = 1
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._:-]+$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class PutOutcome(Enum):
    """Result of an immutable backend write."""

    CREATED = "created"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"


class ReplicaBackend(Protocol):
    """Immutable object backend frozen by the orgfs P2 design."""

    def put(self, key: str, data: bytes) -> PutOutcome: ...

    def get(self, key: str) -> bytes | None: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def keys(self, prefix: str) -> Iterator[str]: ...

    def durable(self) -> bool: ...


class RetirementView(Protocol):
    """Duck-typed read view supplied by the purge/store slice."""

    def retired(self, doc_id: str) -> object | None: ...

    def purge_listed(self, sha: str) -> bool: ...

    def snapshot_point(self, doc_id: str) -> bytes | None: ...


ConflictCallback = Callable[[str, Mapping[str, object]], None]
FrontierDecoder = Callable[[bytes], Mapping[str, int]]


def configured_state_root() -> Path:
    """Return the configured state root without depending on the CLI module."""

    configured = os.environ.get("HARNESS_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    home, _source = configured_hyprial_home()
    return home / "state"


def _segment(value: str, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or value.startswith(".")
        or _SAFE_SEGMENT.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be a safe key segment")
    return value


def _validate_key(key: str) -> tuple[str, ...]:
    if not isinstance(key, str) or not key or key.startswith("/") or key.endswith("/"):
        raise ValueError("replica key must be a non-empty relative object key")
    parts = tuple(key.split("/"))
    for index, part in enumerate(parts):
        _segment(part, name=f"key segment {index}")
    if parts[0] == "log":
        if len(parts) != 5 or not parts[4].isdigit():
            raise ValueError("log key must be log/<space>/<doc>/<writer>/<seq>")
    elif parts[0] == "snapshot":
        if len(parts) != 4:
            raise ValueError("snapshot key must be snapshot/<space>/<doc>/<snapshotId>")
    elif parts[0] == "blob":
        if len(parts) != 2 or _HEX_DIGEST.fullmatch(parts[1]) is None:
            raise ValueError("blob key must be blob/<sha256>")
    else:
        raise ValueError("replica key kind must be log, snapshot, or blob")
    return parts


def _validate_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or prefix.startswith("/") or "//" in prefix:
        raise ValueError("replica prefix must be relative")
    for index, part in enumerate(prefix.rstrip("/").split("/")):
        if part:
            _segment(part, name=f"prefix segment {index}")
    return prefix


def _matches_prefix(key: str, prefix: str) -> bool:
    """Match an object-key prefix without crossing a path-segment boundary."""

    if not prefix or prefix.endswith("/"):
        return key.startswith(prefix)
    return key == prefix or key.startswith(f"{prefix}/")


class MemoryReplicaBackend:
    """Thread-safe non-durable backend used by tests and ephemeral serving."""

    def __init__(self, space_id: str | None = None) -> None:
        self._lock = threading.RLock()
        self.space_id = (
            _segment(space_id, name="space_id") if space_id is not None else None
        )
        self._objects: dict[str, bytes] = {}

    def _key(self, key: str) -> str:
        parts = _validate_key(key)
        if parts[0] in {"log", "snapshot"}:
            with self._lock:
                if self.space_id is None:
                    self.space_id = parts[1]
                elif parts[1] != self.space_id:
                    raise ValueError("replica key belongs to another space")
        return key

    def put(self, key: str, data: bytes) -> PutOutcome:
        key = self._key(key)
        value = bytes(data)
        with self._lock:
            existing = self._objects.get(key)
            if existing is None:
                self._objects[key] = value
                return PutOutcome.CREATED
            if existing == value:
                return PutOutcome.IDEMPOTENT
            return PutOutcome.CONFLICT

    def get(self, key: str) -> bytes | None:
        key = self._key(key)
        with self._lock:
            value = self._objects.get(key)
            return None if value is None else bytes(value)

    def exists(self, key: str) -> bool:
        key = self._key(key)
        with self._lock:
            return key in self._objects

    def delete(self, key: str) -> None:
        key = self._key(key)
        with self._lock:
            self._objects.pop(key, None)

    def keys(self, prefix: str) -> Iterator[str]:
        prefix = _validate_prefix(prefix)
        with self._lock:
            matches = tuple(
                sorted(key for key in self._objects if _matches_prefix(key, prefix))
            )
        return iter(matches)

    def durable(self) -> bool:
        return False


class FsReplicaBackend:
    """Durable immutable-object backend rooted in the selected state tree."""

    def __init__(
        self,
        space_id: str,
        state_root: Path | str | None = None,
    ) -> None:
        self.space_id = _segment(space_id, name="space_id")
        root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None
            else configured_state_root()
        )
        self.root = root / "orgfs" / "replica" / self.space_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_temporary_files()

    def _path(self, key: str) -> Path:
        parts = _validate_key(key)
        if parts[0] in {"log", "snapshot"} and parts[1] != self.space_id:
            raise ValueError("replica key belongs to another space")
        return self.root.joinpath(*parts)

    def _cleanup_temporary_files(self) -> None:
        touched: set[Path] = set()
        for path in self.root.rglob(".*"):
            if path.is_file() and ".tmp-" in path.name:
                path.unlink(missing_ok=True)
                touched.add(path.parent)
        for directory in sorted(touched):
            self._fsync_directory(directory)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def put(self, key: str, data: bytes) -> PutOutcome:
        path = self._path(key)
        value = bytes(data)
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            return PutOutcome.IDEMPOTENT if existing == value else PutOutcome.CONFLICT

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.tmp-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Linking a fully fsynced temporary file creates the final key
                # atomically and fails if another writer won the O_EXCL race.
                os.link(temporary, path)
                self._fsync_directory(path.parent)
                return PutOutcome.CREATED
            except FileExistsError:
                existing = path.read_bytes()
                return (
                    PutOutcome.IDEMPOTENT if existing == value else PutOutcome.CONFLICT
                )
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(path.parent)

    def keys(self, prefix: str) -> Iterator[str]:
        prefix = _validate_prefix(prefix)
        matches = tuple(
            sorted(
                path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*")
                if path.is_file()
                and not path.name.startswith(".")
                and _matches_prefix(path.relative_to(self.root).as_posix(), prefix)
            )
        )
        return iter(matches)

    def durable(self) -> bool:
        return True


def _field(value: object, *names: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _required_text(value: object, *names: str) -> str:
    result = _field(value, *names)
    if not isinstance(result, str) or not result:
        raise ValueError(f"retirement is missing {'/'.join(names)}")
    return result


def encode_snapshot_frontier(watermarks: Mapping[str, int]) -> bytes:
    """Encode the owner-written snapshot frontier shared with the store."""

    writers: dict[str, int] = {}
    for writer, sequence in watermarks.items():
        safe_writer = _segment(writer, name="snapshot writer")
        if type(sequence) is not int or sequence < 0:
            raise ValueError(
                "snapshot sequence watermarks must be non-negative integers"
            )
        writers[safe_writer] = sequence
    return json.dumps(
        {
            "schemaVersion": SNAPSHOT_FRONTIER_SCHEMA_VERSION,
            "writers": writers,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def decode_snapshot_frontier(value: bytes) -> Mapping[str, int]:
    """Decode the owner-written writer-to-sequence snapshot frontier."""

    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("snapshot point needs a frontier decoder") from exc
    if not isinstance(decoded, Mapping) or set(decoded) != {
        "schemaVersion",
        "writers",
    }:
        raise ValueError("snapshot point must use the snapshot frontier schema")
    if decoded.get("schemaVersion") != SNAPSHOT_FRONTIER_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot frontier schema version")
    raw_writers = decoded.get("writers")
    if not isinstance(raw_writers, Mapping):
        raise ValueError("snapshot frontier writers must be an object")
    result: dict[str, int] = {}
    for writer, sequence in raw_writers.items():
        safe_writer = _segment(writer, name="snapshot writer")
        if type(sequence) is not int or sequence < 0:
            raise ValueError(
                "snapshot sequence watermarks must be non-negative integers"
            )
        result[safe_writer] = sequence
    return result


class ReplicaStore:
    """Space-scoped resident replica engine over an immutable backend."""

    def __init__(
        self,
        space_id: str,
        backend: ReplicaBackend,
        retirement_view: RetirementView,
        *,
        event_callback: ConflictCallback | None = None,
        owner_notifier: ConflictCallback | None = None,
        retain_before_snapshot: int = ORGFS_REPLICA_RETAIN_BEFORE_SNAPSHOT,
        frontier_decoder: FrontierDecoder = decode_snapshot_frontier,
    ) -> None:
        self.space_id = _segment(space_id, name="space_id")
        if type(retain_before_snapshot) is not int or retain_before_snapshot < 0:
            raise ValueError("retain_before_snapshot must be a non-negative integer")
        self.backend = backend
        self.retirement_view = retirement_view
        self._event_callback = event_callback
        self._owner_notifier = owner_notifier
        self._retain_before_snapshot = retain_before_snapshot
        self._frontier_decoder = frontier_decoder
        self._pinned_blobs = {
            key.split("/", 1)[1]
            for key in self.backend.keys("blob/")
            if not self.retirement_view.purge_listed(key.split("/", 1)[1])
        }
        self._lock = threading.RLock()

    def durable(self) -> bool:
        return self.backend.durable()

    def pinned_reason(self, digest: str) -> str | None:
        _digest(digest)
        with self._lock:
            return "replica" if digest in self._pinned_blobs else None

    def pinned_blobs(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._pinned_blobs))

    def backend_get(self, key: str) -> bytes | None:
        """Read one raw backend object; diagnostics and tests only."""

        return self.backend.get(key)

    def backend_exists(self, key: str) -> bool:
        """Return whether one raw backend object is present."""

        return self.backend.exists(key)

    def prime_from(self, store: object, blob_store: object | None = None) -> None:
        """Copy an admitted local journal/snapshot/blob set into this backend."""

        for doc_id in store.document_ids():
            cursor: str | None = None
            while True:
                page = store.export_since(
                    doc_id, b"", cursor=cursor, max_bytes=1_048_576
                )
                for envelope in page.envelopes:
                    value = json.loads(envelope)
                    origin = value["origin"]
                    self.store_envelope(
                        f"log/{self.space_id}/{value['docId']}/"
                        f"{origin['writer']}/{value['seq']}",
                        envelope,
                    )
                cursor = page.next
                if cursor is None:
                    break
            manifest = store.snapshot(doc_id, shallow_since=None)
            self.store_snapshot(
                f"snapshot/{self.space_id}/{doc_id}/{manifest.snapshot_id}",
                manifest.snapshot_bytes,
            )
        self.reconcile_blobs(blob_store)

    def reconcile_blobs(self, blob_store: object | None) -> tuple[str, ...]:
        """Copy every locally retained blob reference into this replica."""

        if blob_store is None:
            return ()
        retained = {
            digest
            for _space_id, digest, _reason in blob_store.references(self.space_id)
        }
        reconciled: list[str] = []
        for digest in sorted(retained):
            # Purge is checked before the backend.exists skip so a purged
            # digest is never re-pinned from backend residue.
            if self.retirement_view.purge_listed(digest):
                continue
            if self.backend.exists(f"blob/{digest}"):
                with self._lock:
                    self._pinned_blobs.add(digest)
                continue
            self.store_blob(digest, blob_store.get(self.space_id, digest))
            reconciled.append(digest)
        return tuple(reconciled)

    def _space_key(self, key: str, expected: str) -> tuple[str, ...]:
        parts = _validate_key(key)
        if parts[0] != expected:
            raise ValueError(f"expected a {expected} replica key")
        if expected in {"log", "snapshot"} and parts[1] != self.space_id:
            raise ValueError("replica key belongs to another space")
        return parts

    def _retirement_error(self, doc_id: str, record: object) -> OrgFsError:
        details: dict[str, object] = {"retiredDocId": doc_id}
        replacement = _field(
            record,
            "replacementDocId",
            "replacementPhysicalDocId",
            "replacement_doc_id",
        )
        plan_id = _field(record, "planId", "plan_id")
        if isinstance(replacement, str):
            details["replacementDocId"] = replacement
        if isinstance(plan_id, str):
            details["planId"] = plan_id
        return OrgFsError("snapshot-barrier", details)

    def _check_retired(self, doc_id: str) -> None:
        record = self.retirement_view.retired(doc_id)
        if record is not None:
            raise self._retirement_error(doc_id, record)

    def _put(self, key: str, data: bytes) -> PutOutcome:
        outcome = self.backend.put(key, bytes(data))
        if outcome is not PutOutcome.CONFLICT:
            return outcome
        details: dict[str, object] = {"spaceId": self.space_id, "key": key}
        if self._event_callback is not None:
            self._event_callback("log-key-conflict", details)
        if self._owner_notifier is not None:
            self._owner_notifier("log-key-conflict", details)
        raise OrgFsError("log-key-conflict", details)

    def store_envelope(self, log_key: str, data: bytes) -> PutOutcome:
        parts = self._space_key(log_key, "log")
        self._check_retired(parts[2])
        return self._put(log_key, data)

    def store_snapshot(self, snapshot_key: str, data: bytes) -> PutOutcome:
        parts = self._space_key(snapshot_key, "snapshot")
        self._check_retired(parts[2])
        value = bytes(data)
        if hashlib.sha256(value).hexdigest() != parts[3]:
            raise ValueError("snapshot id must equal the snapshot byte hash")
        return self._put(snapshot_key, value)

    def store_blob(self, digest: str, data: bytes) -> PutOutcome:
        digest = _digest(digest)
        if self.retirement_view.purge_listed(digest):
            raise OrgFsError("purged", {"digest": digest, "spaceId": self.space_id})
        value = bytes(data)
        if hashlib.sha256(value).hexdigest() != digest:
            raise ValueError("blob bytes do not match their content address")
        outcome = self._put(f"blob/{digest}", value)
        if outcome is not PutOutcome.CONFLICT:
            with self._lock:
                self._pinned_blobs.add(digest)
        return outcome

    def serve_log_range(
        self,
        doc_id: str,
        writer: str,
        *,
        after: int | None = None,
        limit: int = 256,
    ) -> tuple[tuple[str, bytes], ...]:
        doc_id = _segment(doc_id, name="doc_id")
        writer = _segment(writer, name="writer")
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError("after must be a non-negative sequence")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        self._check_retired(doc_id)
        prefix = f"log/{self.space_id}/{doc_id}/{writer}/"
        rows: list[tuple[str, bytes]] = []
        for key in sorted(self.backend.keys(prefix), key=_log_sequence):
            sequence = _log_sequence(key)
            if after is not None and sequence <= after:
                continue
            value = self.backend.get(key)
            if value is not None:
                rows.append((key, value))
            if len(rows) == limit:
                break
        return tuple(rows)

    def serve_sync_page(
        self,
        doc_ids: Iterable[str] | None = None,
        *,
        after_key: str | None = None,
        limit: int = 256,
    ) -> tuple[tuple[str, bytes], ...]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        selected = None
        if doc_ids is not None:
            selected = {_segment(doc_id, name="doc_id") for doc_id in doc_ids}
            retired = {
                doc_id: record
                for doc_id in selected
                if (record := self.retirement_view.retired(doc_id)) is not None
            }
            if retired and retired.keys() == selected:
                doc_id = sorted(retired)[0]
                raise self._retirement_error(doc_id, retired[doc_id])
        after_order = None
        if after_key is not None:
            self._space_key(after_key, "log")
            after_order = _log_key_order(after_key)
        rows: list[tuple[str, bytes]] = []
        prefix = f"log/{self.space_id}/"
        for key in sorted(self.backend.keys(prefix), key=_log_key_order):
            parts = _validate_key(key)
            if selected is not None and parts[2] not in selected:
                continue
            if self.retirement_view.retired(parts[2]) is not None:
                continue
            if after_order is not None and _log_key_order(key) <= after_order:
                continue
            value = self.backend.get(key)
            if value is not None:
                rows.append((key, value))
            if len(rows) == limit:
                break
        return tuple(rows)

    def serve_blob_chunk(
        self,
        digest: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> bytes:
        digest = _digest(digest)
        if self.retirement_view.purge_listed(digest):
            raise OrgFsError("purged", {"digest": digest, "spaceId": self.space_id})
        if type(offset) is not int or offset < 0:
            raise OrgFsError("out-of-range", {"offset": offset})
        if length is not None and (type(length) is not int or length <= 0):
            raise OrgFsError("out-of-range", {"length": length})
        value = self.backend.get(f"blob/{digest}")
        if value is None:
            raise OrgFsError("unknown-blob", {"digest": digest})
        if offset > len(value):
            raise OrgFsError("out-of-range", {"offset": offset})
        return value[offset:] if length is None else value[offset : offset + length]

    def apply_retention(self, doc_id: str) -> tuple[str, ...]:
        """Delete log records covered by the injected owner snapshot point."""

        doc_id = _segment(doc_id, name="doc_id")
        point = self.retirement_view.snapshot_point(doc_id)
        if point is None:
            return ()
        watermarks = self._frontier_decoder(point)
        deleted: list[str] = []
        prefix = f"log/{self.space_id}/{doc_id}/"
        for key in tuple(self.backend.keys(prefix)):
            parts = _validate_key(key)
            boundary = watermarks.get(parts[3])
            if boundary is None:
                continue
            if int(parts[4]) <= boundary - self._retain_before_snapshot:
                self.backend.delete(key)
                deleted.append(key)
        return tuple(sorted(deleted))

    def apply_retirement(
        self,
        record: object,
        plan: Mapping[str, object] | object,
    ) -> tuple[str, ...]:
        """Apply the replica side of a whole-document retirement.

        The purge slice supplies the immutable record and exact plan.  The
        integration-only ``replacementSnapshotBytes`` field carries bytes
        already fetched through the normal snapshot path; it is intentionally
        not part of the public purge-plan JSON.
        """

        old_doc_id = _required_text(
            record,
            "oldPhysicalDocId",
            "oldDocId",
            "retiredDocId",
            "old_doc_id",
            "retired_doc_id",
            "doc_id",
        )
        replacement_doc_id = _required_text(
            record,
            "replacementDocId",
            "replacementPhysicalDocId",
            "replacement_doc_id",
        )
        snapshot_id = _required_text(record, "snapshotId", "snapshot_id")
        _segment(old_doc_id, name="retired doc_id")
        _segment(replacement_doc_id, name="replacement doc_id")
        _segment(snapshot_id, name="snapshot_id")

        snapshot_bytes = _field(
            record, "replacementSnapshotBytes", "snapshotBytes", "snapshot_bytes"
        )
        if snapshot_bytes is None:
            snapshot_bytes = _field(
                plan,
                "replacementSnapshotBytes",
                "snapshotBytes",
                "snapshot_bytes",
            )
        if not isinstance(snapshot_bytes, (bytes, bytearray, memoryview)):
            raise ValueError(
                "replacement snapshot bytes are required before retirement"
            )
        snapshot_value = bytes(snapshot_bytes)
        if hashlib.sha256(snapshot_value).hexdigest() != snapshot_id:
            raise ValueError("replacement snapshot bytes do not match snapshotId")

        replacement_key = f"snapshot/{self.space_id}/{replacement_doc_id}/{snapshot_id}"
        self._put(replacement_key, snapshot_value)

        delete_keys = set(self.backend.keys(f"log/{self.space_id}/{old_doc_id}/"))
        delete_keys.update(self.backend.keys(f"snapshot/{self.space_id}/{old_doc_id}/"))

        documents = _field(plan, "docs", default=())
        if isinstance(documents, Iterable) and not isinstance(
            documents, (str, bytes, bytearray, Mapping)
        ):
            for item in documents:
                item_doc = _field(item, "docId", "doc_id")
                if item_doc != old_doc_id:
                    continue
                update_blobs = _field(item, "updateBlobs", "update_blobs", default=())
                if isinstance(update_blobs, Iterable) and not isinstance(
                    update_blobs, (str, bytes, bytearray, Mapping)
                ):
                    for digest in update_blobs:
                        delete_keys.add(f"blob/{_digest(str(digest))}")

        snapshots = _field(plan, "snapshots", default=())
        if isinstance(snapshots, Iterable) and not isinstance(
            snapshots, (str, bytes, bytearray, Mapping)
        ):
            for item in snapshots:
                item_doc = _field(item, "docId", "doc_id")
                item_snapshot = _field(item, "snapshotId", "snapshot_id")
                if item_doc == old_doc_id and isinstance(item_snapshot, str):
                    delete_keys.add(
                        f"snapshot/{self.space_id}/{old_doc_id}/"
                        f"{_segment(item_snapshot, name='snapshot_id')}"
                    )

        blobs = _field(plan, "blobs", default=())
        if isinstance(blobs, Iterable) and not isinstance(
            blobs, (str, bytes, bytearray, Mapping)
        ):
            for item in blobs:
                digest_value = _field(item, "sha", "digest")
                if isinstance(digest_value, str):
                    delete_keys.add(f"blob/{_digest(digest_value)}")

        deleted: list[str] = []
        for key in sorted(delete_keys):
            if self.backend.get(key) is not None:
                deleted.append(key)
            self.backend.delete(key)
            if key.startswith("blob/"):
                with self._lock:
                    self._pinned_blobs.discard(key.split("/", 1)[1])

        residue = tuple(self.backend.keys(f"log/{self.space_id}/{old_doc_id}/"))
        residue += tuple(self.backend.keys(f"snapshot/{self.space_id}/{old_doc_id}/"))
        residue += tuple(
            key for key in delete_keys if self.backend.get(key) is not None
        )
        if residue:
            raise RuntimeError(
                f"retirement byte scan found residue: {sorted(set(residue))}"
            )
        return tuple(deleted)

    def apply_blob_purge(self, digests: Iterable[str]) -> tuple[str, ...]:
        """Delete space-purged blob objects and drop their replica pins."""

        deleted: list[str] = []
        for raw_digest in digests:
            digest = _digest(str(raw_digest))
            key = f"blob/{digest}"
            if self.backend.get(key) is not None:
                deleted.append(key)
            self.backend.delete(key)
            with self._lock:
                self._pinned_blobs.discard(digest)
        return tuple(deleted)


def _digest(value: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None:
        raise ValueError("digest must be a lowercase SHA-256 hex string")
    return value


def _log_sequence(key: str) -> int:
    parts = _validate_key(key)
    if parts[0] != "log":
        raise ValueError("not a log key")
    return int(parts[4])


def _log_key_order(key: str) -> tuple[int, str, str, int]:
    parts = _validate_key(key)
    if parts[0] != "log":
        raise ValueError("not a log key")
    return (
        0 if parts[2] == "meta" else 1,
        parts[2],
        parts[3],
        int(parts[4]),
    )
