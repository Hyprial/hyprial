# Decision record (C1 state store, 2026-11-25):
# - Implements design sections 2.1-2.4 and 2.6 for one space directory.  The
#   store holds no ``pycrdt.Doc`` beyond the throwaway one of ``codec.merge``, no
#   lock and no thread: every call comes from the space's serial executor
#   (section 2.6).  File descriptors are opened per step and closed again.
# - Per document the store caches the scan of its delta (record count and valid
#   length).  A delta is scanned, and a torn tail cut off, the first time the
#   document is loaded, appended to or asked about in this process, so an append
#   never lands behind garbage that a later read would cut away.
# - D2 (C6 report §10 item 4): the scan cache holds each scanned delta's
#   records, so it is a bounded LRU: at most ``SCAN_CACHE_ENTRIES`` documents
#   and ``SCAN_CACHE_BYTES`` of record payload; a scan larger than the byte
#   bound is not kept.  An evicted document is simply scanned again on its
#   next use (torn-tail repair included), so eviction never changes results.
# - ``commit`` (section 2.4): a single append without deletes is written to the
#   delta and fsynced; when that fails the delta is truncated back (or the new
#   file removed).  Every other batch is written to ``batch.pending`` first
#   (temp, fsync, rename, fsync directory); then the appends, the deletions and
#   the removal of ``batch.pending`` follow.  A failure before the rename leaves
#   nothing behind and the store usable.  A failure after the rename, or a failed
#   rollback, leaves the outcome to recovery: the store then raises
#   ``StateStoreBroken`` for every call until it is reopened, because its view of
#   the files is no longer known to match the disk.  The caller treats the batch
#   as unconfirmed either way (section 2.5).
# - Recovery at open redoes a ``batch.pending`` whose checksum is right and
#   deletes one whose checksum is wrong (torn before its rename), logging
#   ``orgfs.state.batch-discarded``.  The layout check (section 2.7) runs before
#   recovery so an old-layout directory is never written to; section 2.3 lists
#   recovery first, which differs only for directories that are refused anyway.
# - ``compact`` writes the new state with temp, fsync, rename, fsync directory
#   and then truncates the delta to its magic and fsyncs it.  Between the two the
#   delta replays idempotently onto the new state.  A failed compaction never
#   breaks the store: the old files, or the new state plus a replayable delta,
#   stay valid; only the cached delta scan is dropped.  When no state is given
#   the store merges state and delta itself; a given state is trusted to contain
#   every delta record (it comes from the authoritative Doc, section 2.3).
# - There is no ``document_ids``: content file names are a one-way hash of the
#   doc id and the files do not store it; the tree says which documents exist
#   (design 2.6 as revised in stage C).
# - Corrupt files (foreign magic, a checksummed batch with an unknown shape) are
#   refused with ``ORGFS_LAYOUT_UNSUPPORTED`` and reason ``corrupt-file`` and are
#   never rewritten; the design names no dedicated code (C1 report).
"""``FileStateStore``: the file-backed ``StateStore`` of one space."""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.state.codec import (
    DELTA_MAGIC,
    MAGIC_BYTES,
    CorruptStateFile,
    DeltaScan,
    decode_batch,
    decode_state,
    encode_batch,
    encode_record,
    encode_state,
    merge,
    require_doc_id,
    scan_delta,
    validate_batch,
)
from hyprial.daemon.impl.orgfs.state.fileops import FileOps
from hyprial.daemon.impl.orgfs.state.interface import (
    COMPACTION_BYTES,
    COMPACTION_RECORDS,
    Batch,
    StateStoreBroken,
)
from hyprial.daemon.impl.orgfs.state.layout import (
    DOCS_DIR,
    PENDING_FILE,
    SPACE_FILE,
    check_space_json,
    document_paths,
    is_space_directory_name,
    space_directory_kind,
    space_json_bytes,
    temp_path,
    unsupported,
)

_Logger = Callable[..., None]
SCAN_CACHE_ENTRIES = 256
SCAN_CACHE_BYTES = 32 * 1024 * 1024
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
_APPEND_FLAGS = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)


class _ScanCache:
    """LRU of delta scans bounded by entries and record payload bytes."""

    def __init__(self, entries: int = SCAN_CACHE_ENTRIES, size: int = SCAN_CACHE_BYTES) -> None:
        self._entries = entries
        self._size = size
        self._bytes = 0
        self._scans: OrderedDict[str, DeltaScan] = OrderedDict()

    def get(self, doc_id: str) -> DeltaScan | None:
        scan = self._scans.get(doc_id)
        if scan is not None:
            self._scans.move_to_end(doc_id)
        return scan

    def __setitem__(self, doc_id: str, scan: DeltaScan) -> None:
        self.pop(doc_id, None)
        if scan.payload_bytes > self._size:
            return
        self._scans[doc_id] = scan
        self._bytes += scan.payload_bytes
        while len(self._scans) > self._entries or self._bytes > self._size:
            _doc_id, evicted = self._scans.popitem(last=False)
            self._bytes -= evicted.payload_bytes

    def pop(self, doc_id: str, default: None = None) -> DeltaScan | None:
        scan = self._scans.pop(doc_id, None)
        if scan is not None:
            self._bytes -= scan.payload_bytes
        return scan

    def clear(self) -> None:
        self._scans.clear()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._scans)

    def values(self) -> tuple[DeltaScan, ...]:
        return tuple(self._scans.values())


class FileStateStore:
    """State and delta files of one space under ``spaces/<spaceId>/``."""

    def __init__(self, space_dir: Path, ops: FileOps, logger: _Logger | None) -> None:
        self.space_id = space_dir.name
        self.recovered_batch = False
        self._dir = space_dir
        self._ops = ops
        self._logger = logger
        self._scans = _ScanCache()
        self._closed = False
        self._broken: str | None = None

    @classmethod
    def open(
        cls,
        space_dir: str | os.PathLike[str],
        *,
        create: bool = False,
        ops: FileOps | None = None,
        logger: _Logger | None = None,
    ) -> FileStateStore:
        """Open (or with ``create`` make) the space stored in ``space_dir``."""

        path = Path(space_dir)
        if not is_space_directory_name(path.name):
            raise ValueError(f"space directory must be named by a UUID: {path}")
        store = cls(path, ops or FileOps(), logger)
        if not path.is_dir() or space_directory_kind(path) == "uninitialized":
            if not create:
                raise FileNotFoundError(path / SPACE_FILE)
            store._create_space()
        check_space_json(path, store.space_id)
        store._recover()
        return store

    def load(self, doc_id: str) -> bytes | None:
        self._check_usable()
        require_doc_id(doc_id)
        state_path, _delta_path = document_paths(self._dir, doc_id)
        scan = self._scan(doc_id)
        state = self._read_state(state_path)
        parts = [] if state is None else [state]
        return merge(parts + list(() if scan is None else scan.records))

    def commit(self, batch: Batch) -> None:
        self._check_usable()
        validate_batch(batch)
        if not batch.appends and not batch.deletes:
            return
        if len(batch.appends) == 1 and not batch.deletes:
            doc_id, update = batch.appends[0]
            self._append_or_rollback(doc_id, update)
            return
        self._write_pending(batch)
        try:
            self._apply(batch)
            self._remove_pending()
        except Exception as exc:
            self._broken = f"batch failed after it became durable: {exc}"
            raise

    def needs_compaction(self, doc_id: str) -> bool:
        self._check_usable()
        require_doc_id(doc_id)
        scan = self._scan(doc_id)
        if scan is None:
            return False
        return len(scan.records) >= COMPACTION_RECORDS or scan.payload_bytes >= COMPACTION_BYTES

    def compact(self, doc_id: str, state: bytes | None = None) -> None:
        self._check_usable()
        require_doc_id(doc_id)
        state_path, delta_path = document_paths(self._dir, doc_id)
        scan = self._scan(doc_id)
        if scan is None and not state_path.exists():
            return
        if state is None:
            state = self.load(doc_id)
            if state is None:
                return
        self._replace(state_path, encode_state(state), "state")
        if scan is None or not scan.has_magic:
            return
        try:
            fd = os.open(delta_path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
            try:
                self._ops.truncate(fd, MAGIC_BYTES, "delta.compact-truncate")
                self._scans.pop(doc_id, None)
                self._ops.fsync(fd, "delta.compact-fsync")
            finally:
                os.close(fd)
        except Exception:
            self._scans.pop(doc_id, None)
            raise
        self._scans[doc_id] = DeltaScan((), MAGIC_BYTES, MAGIC_BYTES, True)

    def close(self) -> None:
        self._closed = True
        self._scans.clear()

    def _check_usable(self) -> None:
        if self._closed:
            raise RuntimeError(f"state store of space {self.space_id} is closed")
        if self._broken is not None:
            raise StateStoreBroken(f"state store of space {self.space_id} must be reopened: {self._broken}")

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, event, spaceId=self.space_id, **fields)
        except Exception:
            return

    def _corrupt(self, path: Path, exc: Exception) -> Exception:
        error = unsupported(self.space_id, "corrupt-file")
        error.details["path"] = str(path)
        error.__cause__ = exc
        return error

    def _create_space(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / DOCS_DIR).mkdir(exist_ok=True)
        self._replace(self._dir / SPACE_FILE, space_json_bytes(self.space_id), "space")
        self._ops.fsync_dir(self._dir.parent, "space.fsync-parent")

    def _replace(self, path: Path, data: bytes, label: str) -> None:
        temp = temp_path(path)
        renamed = False
        try:
            fd = os.open(temp, _WRITE_FLAGS, 0o600)
            try:
                self._ops.write(fd, data, f"{label}.write")
                self._ops.fsync(fd, f"{label}.fsync")
            finally:
                os.close(fd)
            self._ops.rename(temp, path, f"{label}.rename")
            renamed = True
            self._ops.fsync_dir(path.parent, f"{label}.fsync-dir")
        except Exception:
            if not renamed:
                try:
                    self._ops.unlink(temp, f"{label}.discard")
                except Exception:
                    pass
            raise

    def _read_state(self, path: Path) -> bytes | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return decode_state(raw)
        except CorruptStateFile as exc:
            raise self._corrupt(path, exc) from exc

    def _scan(self, doc_id: str) -> DeltaScan | None:
        cached = self._scans.get(doc_id)
        if cached is not None:
            return cached
        _state_path, delta_path = document_paths(self._dir, doc_id)
        try:
            raw = delta_path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            scan = scan_delta(raw)
        except CorruptStateFile as exc:
            raise self._corrupt(delta_path, exc) from exc
        if scan.torn:
            self._truncate_tail(delta_path, scan)
            self._log(
                "warn",
                "orgfs.state.delta-truncated",
                docId=doc_id,
                validBytes=scan.valid_bytes,
                fileBytes=scan.file_bytes,
            )
            scan = DeltaScan(scan.records, scan.valid_bytes, scan.valid_bytes, scan.has_magic)
        self._scans[doc_id] = scan
        return scan

    def _truncate_tail(self, path: Path, scan: DeltaScan) -> None:
        fd = os.open(path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            self._ops.truncate(fd, scan.valid_bytes, "delta.repair-truncate")
            self._ops.fsync(fd, "delta.repair-fsync")
        finally:
            os.close(fd)

    def _append(self, doc_id: str, update: bytes) -> tuple[Path, int, bool]:
        """Append one record; returns ``(delta path, previous length, created)``."""

        scan = self._scan(doc_id)
        _state_path, delta_path = document_paths(self._dir, doc_id)
        created = scan is None
        previous = 0 if scan is None else scan.valid_bytes
        needs_magic = scan is None or not scan.has_magic
        record = encode_record(update)
        data = DELTA_MAGIC + record if needs_magic else record
        self._scans.pop(doc_id, None)
        fd = os.open(delta_path, _APPEND_FLAGS, 0o600)
        try:
            os.lseek(fd, previous, os.SEEK_SET)
            self._ops.write(fd, data, "delta.write")
            self._ops.fsync(fd, "delta.fsync")
        finally:
            os.close(fd)
        if created:
            self._ops.fsync_dir(delta_path.parent, "delta.fsync-dir")
        records = () if scan is None else scan.records
        end = MAGIC_BYTES + len(record) if needs_magic else previous + len(record)
        self._scans[doc_id] = DeltaScan(records + (update,), end, end, True)
        return delta_path, previous, created

    def _append_or_rollback(self, doc_id: str, update: bytes) -> None:
        scan = self._scan(doc_id)
        _state_path, delta_path = document_paths(self._dir, doc_id)
        previous = 0 if scan is None else scan.valid_bytes
        try:
            self._append(doc_id, update)
        except Exception:
            self._scans.pop(doc_id, None)
            try:
                self._rollback(delta_path, previous, created=scan is None)
            except Exception as rollback:
                self._broken = f"rollback of a failed append failed: {rollback}"
            raise

    def _rollback(self, delta_path: Path, previous: int, *, created: bool) -> None:
        if created:
            if self._ops.unlink(delta_path, "delta.rollback-unlink"):
                self._ops.fsync_dir(delta_path.parent, "delta.rollback-fsync-dir")
            return
        fd = os.open(delta_path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            self._ops.truncate(fd, previous, "delta.rollback-truncate")
            self._ops.fsync(fd, "delta.rollback-fsync")
        finally:
            os.close(fd)

    def _delete(self, doc_id: str) -> None:
        state_path, delta_path = document_paths(self._dir, doc_id)
        self._scans.pop(doc_id, None)
        removed = self._ops.unlink(state_path, "doc.unlink-state")
        removed = self._ops.unlink(delta_path, "doc.unlink-delta") or removed
        if removed:
            self._ops.fsync_dir(delta_path.parent, "doc.fsync-dir")

    def _apply(self, batch: Batch) -> None:
        for doc_id, update in batch.appends:
            self._append(doc_id, update)
        for doc_id in batch.deletes:
            self._delete(doc_id)

    def _write_pending(self, batch: Batch) -> None:
        pending = self._dir / PENDING_FILE
        temp = temp_path(pending)
        renamed = False
        try:
            fd = os.open(temp, _WRITE_FLAGS, 0o600)
            try:
                self._ops.write(fd, encode_batch(batch), "pending.write")
                self._ops.fsync(fd, "pending.fsync")
            finally:
                os.close(fd)
            self._ops.rename(temp, pending, "pending.rename")
            renamed = True
            self._ops.fsync_dir(self._dir, "pending.fsync-dir")
        except Exception as exc:
            if renamed:
                self._broken = f"batch failed after it became durable: {exc}"
                raise
            try:
                self._ops.unlink(temp, "pending.discard")
            except Exception:
                pass
            raise

    def _remove_pending(self) -> None:
        self._ops.unlink(self._dir / PENDING_FILE, "pending.unlink")
        self._ops.fsync_dir(self._dir, "pending.unlink-fsync-dir")

    def _recover(self) -> None:
        pending = self._dir / PENDING_FILE
        if self._ops.unlink(temp_path(pending), "pending.discard"):
            self._ops.fsync_dir(self._dir, "pending.discard-fsync-dir")
        try:
            raw = pending.read_bytes()
        except FileNotFoundError:
            return
        try:
            batch = decode_batch(raw)
        except CorruptStateFile as exc:
            raise self._corrupt(pending, exc) from exc
        if batch is None:
            self._log("warn", "orgfs.state.batch-discarded", bytes=len(raw))
            self._remove_pending()
            return
        self._apply(batch)
        self._remove_pending()
        self.recovered_batch = True
        self._log(
            "info",
            "orgfs.state.batch-recovered",
            appends=len(batch.appends),
            deletes=len(batch.deletes),
        )
