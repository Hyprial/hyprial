# Decision record (C1 state store, 2026-11-25):
# - Same Protocol and observable behaviour as ``FileStateStore`` (the C1 tests
#   run one behaviour suite against both), kept in the product package so the
#   other stage C packages and stage D can build on it without a file system.
# - State lives in a ``MemoryBacking`` that outlives a store; opening a second
#   store on the same backing stands for a process restart.  The backing counts
#   commits, loads and compactions for tests that assert on work done.
# - Failures are one-shot and named: ``commit`` fails before anything changes;
#   ``commit.after`` applies the batch and then raises, the shape of a failure
#   after ``batch.pending`` became durable, so the store raises
#   ``StateStoreBroken`` until reopened and the next store reports
#   ``recovered_batch``; ``compact``, ``compact.after`` and ``load`` behave alike.
#   The default error is ``OSError(EIO)``.
# - Merging and the compaction thresholds are those of the file store; the delta
#   size counts each record as 8 header bytes plus the update, like the file.
"""``MemoryStateStore``: an in-memory ``StateStore`` with injectable failures."""

from __future__ import annotations

import errno
from dataclasses import dataclass, field

from hyprial.daemon.impl.orgfs.state.codec import HEADER_BYTES, merge, require_doc_id, validate_batch
from hyprial.daemon.impl.orgfs.state.interface import (
    COMPACTION_BYTES,
    COMPACTION_RECORDS,
    Batch,
    StateStoreBroken,
)

FAILURE_POINTS = frozenset({"commit", "commit.after", "compact", "compact.after", "load"})


@dataclass
class _Document:
    state: bytes | None = None
    delta: list[bytes] = field(default_factory=list)


@dataclass
class MemoryBacking:
    """What survives a simulated restart of a ``MemoryStateStore``."""

    space_id: str
    documents: dict[str, _Document] = field(default_factory=dict)
    redo_pending: bool = False
    commits: int = 0
    loads: int = 0
    compactions: int = 0


class MemoryStateStore:
    """In-memory ``StateStore``; pass ``backing`` to reopen the same space."""

    def __init__(self, space_id: str, *, backing: MemoryBacking | None = None) -> None:
        if backing is None:
            backing = MemoryBacking(space_id)
        elif backing.space_id != space_id:
            raise ValueError(f"backing belongs to space {backing.space_id}, not {space_id}")
        self.space_id = space_id
        self.backing = backing
        self.recovered_batch = backing.redo_pending
        backing.redo_pending = False
        self._failures: dict[str, BaseException] = {}
        self._closed = False
        self._broken = False

    def fail_next(self, point: str, error: BaseException | None = None) -> None:
        """Make the next operation at ``point`` raise ``error`` (default ``OSError``)."""

        if point not in FAILURE_POINTS:
            raise ValueError(f"unknown failure point {point!r}; known: {sorted(FAILURE_POINTS)}")
        self._failures[point] = error or OSError(errno.EIO, f"injected failure at {point}")

    def load(self, doc_id: str) -> bytes | None:
        self._check_usable()
        require_doc_id(doc_id)
        self._fire("load")
        self.backing.loads += 1
        document = self.backing.documents.get(doc_id)
        if document is None:
            return None
        parts = ([document.state] if document.state is not None else []) + document.delta
        return merge(parts)

    def commit(self, batch: Batch) -> None:
        self._check_usable()
        validate_batch(batch)
        if not batch.appends and not batch.deletes:
            return
        self._fire("commit")
        for doc_id, update in batch.appends:
            self.backing.documents.setdefault(doc_id, _Document()).delta.append(bytes(update))
        for doc_id in batch.deletes:
            self.backing.documents.pop(doc_id, None)
        self.backing.commits += 1
        if "commit.after" in self._failures:
            self._broken = True
            self.backing.redo_pending = True
            self._fire("commit.after")

    def needs_compaction(self, doc_id: str) -> bool:
        self._check_usable()
        require_doc_id(doc_id)
        document = self.backing.documents.get(doc_id)
        if document is None:
            return False
        size = sum(HEADER_BYTES + len(update) for update in document.delta)
        return len(document.delta) >= COMPACTION_RECORDS or size >= COMPACTION_BYTES

    def compact(self, doc_id: str, state: bytes | None = None) -> None:
        self._check_usable()
        require_doc_id(doc_id)
        document = self.backing.documents.get(doc_id)
        if document is None:
            return
        self._fire("compact")
        if state is None:
            parts = ([document.state] if document.state is not None else []) + document.delta
            state = merge(parts)
        if state is not None:
            document.state = bytes(state)
            document.delta = []
        self.backing.compactions += 1
        self._fire("compact.after")

    def close(self) -> None:
        self._closed = True

    def _fire(self, point: str) -> None:
        error = self._failures.pop(point, None)
        if error is not None:
            raise error

    def _check_usable(self) -> None:
        if self._closed:
            raise RuntimeError(f"state store of space {self.space_id} is closed")
        if self._broken:
            raise StateStoreBroken(f"state store of space {self.space_id} must be reopened")

