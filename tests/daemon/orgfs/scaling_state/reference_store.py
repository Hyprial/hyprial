"""Minimal reference ``StateStore`` used only as the object under test's stand-in.

Decisions (C6, 2026-11-25):

* Implements ``orgfs-minimal-design-2026-11-25.md`` §2.2 (file formats), §2.3
  (read, torn tail truncation, compaction) and §2.4 (single append fast path,
  ``batch.pending`` for everything else, recovery on open) literally.  It is
  not product code; C1's real store replaces it as a ``state_store_factory``
  parameter.
* ``defect`` injects one deliberate scaling bug (``open-scan``, ``commit-rewrite``,
  ``load-scan``, ``no-compact``, ``compact-concat``, ``fsync-all``) so the
  scaling tests can be shown to fail for the right reason.  ``None`` is the
  correct behaviour and the only value used in normal runs.
* A first append to a document creates ``docs/<stem>.delta`` and so also
  fsyncs the ``docs`` directory; the design is silent on that fsync (see the
  C6 report, "设计问题").
* The store never holds a ``Doc`` between calls.  ``compact(doc_id, None)``
  builds a throwaway one, as §2.3 says.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import struct
from typing import Iterable

from pycrdt import Doc, merge_updates

from hyprial.daemon.impl.orgfs.state import Batch


STATE_MAGIC = b"ORGFSST3"
DELTA_MAGIC = b"ORGFSDL3"
BATCH_MAGIC = b"ORGFSBT3"
COMPACT_RECORDS = 500
COMPACT_BYTES = 1 << 20
_HEADER = struct.Struct(">II")


def document_stem(doc_id: str) -> str:
    digest = hashlib.sha256(doc_id.encode("utf-8")).digest()
    return "h-" + base64.b32encode(digest).decode("ascii").lower()[:32]


def _crc(payload: bytes) -> int:
    return binascii.crc32(payload) & 0xFFFFFFFF


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class ReferenceStateStore:
    """§2.6 ``StateStore`` over the §2.2 formats; one instance per space directory."""

    def __init__(self, space_dir: Path, *, defect: str | None = None) -> None:
        self.space_id = space_dir.name
        self.recovered_batch = False
        self._dir = space_dir
        self._docs = space_dir / "docs"
        self._defect = defect
        self._delta_stats: dict[str, tuple[int, int]] = {}
        self._dir.mkdir(parents=True, exist_ok=True)
        self._recover_batch()
        if defect == "open-scan" and self._docs.exists():
            for name in sorted(os.listdir(self._docs)):
                (self._docs / name).read_bytes()

    def _paths(self, doc_id: str) -> tuple[Path, Path]:
        if doc_id in ("meta", "tree"):
            return self._dir / f"{doc_id}.state", self._dir / f"{doc_id}.delta"
        stem = document_stem(doc_id)
        return self._docs / f"{stem}.state", self._docs / f"{stem}.delta"

    def document_ids(self) -> tuple[str, ...]:
        raise NotImplementedError("doc ids are hashed on disk; the reference store keeps no index")

    def _recover_batch(self) -> None:
        pending = self._dir / "batch.pending"
        if not pending.exists():
            return
        raw = pending.read_bytes()
        batch = self._parse_batch(raw)
        if batch is not None:
            self._execute(batch)
            self.recovered_batch = True
        os.unlink(pending)
        _fsync_dir(self._dir)

    @staticmethod
    def _parse_batch(raw: bytes) -> Batch | None:
        if raw[:8] != BATCH_MAGIC or len(raw) < 16:
            return None
        length, crc = _HEADER.unpack_from(raw, 8)
        body = raw[16 : 16 + length]
        if len(body) != length or _crc(body) != crc:
            return None
        parsed = json.loads(body)
        return Batch(
            appends=tuple((doc, base64.b64decode(blob)) for doc, blob in parsed["appends"]),
            deletes=tuple(parsed["deletes"]),
        )

    def _scan_delta(self, doc_id: str) -> list[bytes]:
        _, delta = self._paths(doc_id)
        if not delta.exists():
            return []
        raw = delta.read_bytes()
        records: list[bytes] = []
        offset = len(DELTA_MAGIC)
        valid = raw[: len(DELTA_MAGIC)] == DELTA_MAGIC
        if not valid:
            offset = 0
        while valid and offset < len(raw):
            if offset + _HEADER.size > len(raw):
                break
            length, crc = _HEADER.unpack_from(raw, offset)
            body = raw[offset + _HEADER.size : offset + _HEADER.size + length]
            if len(body) != length or _crc(body) != crc:
                break
            records.append(body)
            offset += _HEADER.size + length
        if not valid:
            _write_file_atomic(delta, DELTA_MAGIC)
            self._delta_stats[doc_id] = (0, 0)
            return []
        if offset < len(raw):
            with open(delta, "r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
        self._delta_stats[doc_id] = (len(records), offset - len(DELTA_MAGIC))
        return records

    def load(self, doc_id: str) -> bytes | None:
        if self._defect == "load-scan" and self._docs.exists():
            for name in sorted(os.listdir(self._docs)):
                (self._docs / name).read_bytes()
        state_path, _ = self._paths(doc_id)
        parts: list[bytes] = []
        if state_path.exists():
            raw = state_path.read_bytes()
            if raw[:8] != STATE_MAGIC:
                raise ValueError(f"bad state magic for {doc_id}")
            parts.append(raw[8:])
        parts.extend(self._scan_delta(doc_id))
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else merge_updates(*parts)

    def _append(self, doc_id: str, update: bytes) -> None:
        _, delta = self._paths(doc_id)
        fresh = not delta.exists()
        if fresh:
            delta.parent.mkdir(parents=True, exist_ok=True)
        record = _HEADER.pack(len(update), _crc(update)) + update
        with open(delta, "ab") as handle:
            if fresh:
                handle.write(DELTA_MAGIC)
            handle.write(record)
            handle.flush()
            os.fsync(handle.fileno())
        if fresh:
            _fsync_dir(delta.parent)
        count, size = self._delta_stats.get(doc_id, (0, 0)) if not fresh else (0, 0)
        self._delta_stats[doc_id] = (count + 1, size + len(record))

    def _delete(self, doc_id: str) -> None:
        for path in self._paths(doc_id):
            if path.exists():
                os.unlink(path)
        self._delta_stats.pop(doc_id, None)
        _fsync_dir(self._docs if self._docs.exists() else self._dir)

    def _execute(self, batch: Batch) -> None:
        for doc_id, update in batch.appends:
            self._append(doc_id, update)
        for doc_id in batch.deletes:
            self._delete(doc_id)

    def commit(self, batch: Batch) -> None:
        if self._defect == "commit-rewrite" and self._docs.exists():
            for name in sorted(os.listdir(self._docs)):
                path = self._docs / name
                _write_file_atomic(path, path.read_bytes())
        if self._defect == "fsync-all" and self._docs.exists():
            for name in sorted(os.listdir(self._docs)):
                with open(self._docs / name, "rb") as handle:
                    os.fsync(handle.fileno())
        if len(batch.appends) == 1 and not batch.deletes:
            doc_id, update = batch.appends[0]
            self._append(doc_id, update)
            return
        if not batch.appends and not batch.deletes:
            return
        body = json.dumps(
            {
                "appends": [[doc, base64.b64encode(blob).decode("ascii")] for doc, blob in batch.appends],
                "deletes": list(batch.deletes),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        pending = self._dir / "batch.pending"
        _write_file_atomic(pending, BATCH_MAGIC + _HEADER.pack(len(body), _crc(body)) + body)
        self._execute(batch)
        os.unlink(pending)
        _fsync_dir(self._dir)

    def needs_compaction(self, doc_id: str) -> bool:
        if self._defect == "no-compact":
            return False
        if doc_id not in self._delta_stats:
            self._scan_delta(doc_id)
        count, size = self._delta_stats.get(doc_id, (0, 0))
        return count >= COMPACT_RECORDS or size >= COMPACT_BYTES

    def compact(self, doc_id: str, state: bytes | None = None) -> None:
        state_path, delta = self._paths(doc_id)
        if state is None:
            merged = self.load(doc_id)
            if merged is None:
                return
            scratch = Doc()
            scratch.apply_update(merged)
            state = scratch.get_update()
        if self._defect == "compact-concat":
            old = state_path.read_bytes()[8:] if state_path.exists() else b""
            state = old + state
        state_path.parent.mkdir(parents=True, exist_ok=True)
        _write_file_atomic(state_path, STATE_MAGIC + state)
        with open(delta, "wb") as handle:
            handle.write(DELTA_MAGIC)
            handle.flush()
            os.fsync(handle.fileno())
        self._delta_stats[doc_id] = (0, 0)

    def close(self) -> None:
        return None

    def disk_bytes(self, doc_id: str) -> int:
        return sum(p.stat().st_size for p in self._paths(doc_id) if p.exists())


def populate_documents(store: ReferenceStateStore, updates: Iterable[tuple[str, bytes]]) -> None:
    for doc_id, update in updates:
        store.commit(Batch(appends=((doc_id, update),)))
