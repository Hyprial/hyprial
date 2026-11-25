"""Append-only conversation record segments in the owner-home OrgFS space.

Decision notes (phase A, S6; plan §2.0 "S6 placement", §3 "OrgFS"):

- Records live under ``cells/conversation/<c.id>/rec-<writer>/<seq>.jsonl``
  in the owner-home space.  ``conversation`` is a storage category, not a
  cell kind (owner ruling 2026-11-16): no cell document is written or read
  there, and a ``cells/conversation/<c.id>.json`` left by an earlier build
  is never decoded or deleted.  ``writer`` is this daemon's node id, so two
  devices of one owner never write the same file; readers of a conversation
  take the union of every ``rec-*``.
- A segment only ever grows by whole lines.  Each append reads the segment,
  skips entries whose key is already present (a crash after the write but
  before the local index commit), writes with ``expect_version`` (or
  ``create_only`` for a new segment) and reads the segment back; only
  entries that read back byte-identical are reported as recorded.
- A segment rolls over at ``SEGMENT_MAX_ENTRIES`` entries or
  ``SEGMENT_MAX_BYTES``; one entry larger than that gets a segment of its
  own, and an entry over ``ENTRY_MAX_BYTES`` is never written (the recorder
  writes a bodiless placeholder instead and the body stays local).  Both
  stay far below OrgFS's 4 MiB text limit.
- Every OrgFS failure on the write path is ``RecordUnavailable``: the
  recorder retries the rows later and never drops a body for them.  Reads
  for hydration never raise; anything unreadable answers None.
- Reads remember a few parsed segments by (space, path, version), so a
  history page that hydrates many entries of one segment parses it once.

Decision notes (slice GC1, group conversations):

- The append discipline is the module function ``append_entries``, generic
  over the line parser and the segment path, so the group log
  (``conversations.groups``) appends with exactly the same rules.
  ``SegmentStore`` calls it unchanged; only where its record refs are built
  moved.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

from hyprial.daemon.impl.cells.runtime.layout import CELLS_ROOT, kind_directory
from hyprial.daemon.impl.conversations.record.contracts import (
    CONVERSATION_KIND,
    RecordEntry,
    RecordRef,
    RecordUnavailable,
    parse_segment,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError

SEGMENT_MAX_BYTES = 256 * 1024
SEGMENT_MAX_ENTRIES = 256
ENTRY_MAX_BYTES = 3 * 1024 * 1024
READ_CACHE_SEGMENTS = 8
_ABSENT_CODES = frozenset({"unknown-doc", "content_pending", "ORGFS_CONTENT_PENDING"})
_EXISTS_CODES = frozenset({"invalid-argument", "ambiguous-path"})
_WRITER = re.compile(r"[A-Za-z0-9._-]{1,64}")

Recorded = dict[str, tuple[RecordRef, RecordEntry]]
Parse = Callable[[str], dict[str, Any]]


def writer_name(writer: str) -> str:
    """*writer* when it is a safe path segment, else a short hash of it."""

    if _WRITER.fullmatch(writer) is None:
        return hashlib.sha256(writer.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return writer


def writer_segment(writer: str) -> str:
    """The ``rec-<writer>`` directory name for one writer id."""

    return f"rec-{writer_name(writer)}"


def make_directories(fs: Any, space: str, paths: Sequence[str]) -> None:
    """Create each directory of *paths* in order; an existing one is fine."""

    for path in paths:
        try:
            fs.mkdir(space, path)
        except OrgFsError as error:
            if error.code not in _EXISTS_CODES:
                raise


def append_entries(
    fs: Any,
    space: str,
    path_at: Callable[[int], str],
    entries: Sequence[Any],
    seq: int,
    parse: Parse,
) -> tuple[dict[str, tuple[str, Any]], int]:
    """Append *entries* from segment *seq* on; what reads back by key (with its path) and the seq.

    An entry needs ``key``, ``to_line()`` and equality; *parse* maps a
    segment's text to its entries by key.
    """

    recorded: dict[str, tuple[str, Any]] = {}
    pending = list(entries)
    while pending:
        path = path_at(seq)
        content, version = _current(fs, space, path)
        present = parse(content)
        size = len(content.encode("utf-8"))
        count = len(present)
        taken: list[Any] = []
        lines: list[str] = []
        rest: list[Any] = []
        for index, entry in enumerate(pending):
            if entry.key in present:
                continue
            line = entry.to_line()
            length = len(line.encode("utf-8"))
            if count and (count >= SEGMENT_MAX_ENTRIES or size + length > SEGMENT_MAX_BYTES):
                rest = pending[index:]
                break
            taken.append(entry)
            lines.append(line)
            size += length
            count += 1
        if taken:
            _write(fs, space, path, content, version, lines, taken, parse)
        done = pending[: len(pending) - len(rest)]
        for entry in done:
            recorded[entry.key] = (path, present.get(entry.key, entry))
        if rest:
            seq += 1
        pending = rest
    return recorded, seq


def _write(
    fs: Any,
    space: str,
    path: str,
    content: str,
    version: str | None,
    lines: list[str],
    taken: list[Any],
    parse: Parse,
) -> None:
    prefix = content if not content or content.endswith("\n") else content + "\n"
    options: dict[str, Any] = (
        {"create_only": True} if version is None else {"expect_version": version}
    )
    fs.write_text(space, path, prefix + "".join(lines), **options)
    back, _ = fs.read_text(space, path)
    stored = parse(back)
    if any(stored.get(entry.key) != entry for entry in taken):
        raise RecordUnavailable("read-back-mismatch")


def _current(fs: Any, space: str, path: str) -> tuple[str, str | None]:
    try:
        snapshot = fs.read_text_snapshot(space, path)
    except OrgFsError as error:
        if error.code in _ABSENT_CODES:
            return "", None
        raise
    return str(snapshot.content), str(snapshot.version)


class SegmentStore:
    """Reads and appends conversation record segments over one OrgFS facade."""

    def __init__(
        self,
        *,
        fs: Callable[[], Any],
        space_for: Callable[[str], str | None],
        writer: str,
    ) -> None:
        self._fs = fs
        self._space_for = space_for
        self._writer = writer_segment(writer)
        self._directories: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str], tuple[str, dict[str, RecordEntry]]] = (
            OrderedDict()
        )

    def directory(self, cell_id: str) -> str:
        return f"{kind_directory(CONVERSATION_KIND)}/{cell_id}/{self._writer}"

    def segment_path(self, cell_id: str, seq: int) -> str:
        return f"{self.directory(cell_id)}/{seq:06d}.jsonl"

    def append(
        self, authority: str, cell_id: str, entries: Sequence[RecordEntry], seq: int
    ) -> tuple[Recorded, int]:
        """Append *entries* to the conversation's record; returns what reads back and the seq."""

        try:
            return self._append(authority, cell_id, entries, seq)
        except RecordUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - any OrgFS failure only delays the record
            code = getattr(error, "code", type(error).__name__)
            raise RecordUnavailable(str(code)) from error

    def _append(
        self, authority: str, cell_id: str, entries: Sequence[RecordEntry], seq: int
    ) -> tuple[Recorded, int]:
        fs = self._fs()
        space = self._space_for(authority)
        if not space:
            raise RecordUnavailable("no-owner-home-space")
        self._ensure_directories(fs, space, cell_id)
        stored, seq = append_entries(
            fs, space, lambda at: self.segment_path(cell_id, at), entries, seq, parse_segment
        )
        recorded: Recorded = {
            key: (RecordRef(space, path, key), entry) for key, (path, entry) in stored.items()
        }
        return recorded, seq

    def _ensure_directories(self, fs: Any, space: str, cell_id: str) -> None:
        if (space, cell_id) in self._directories:
            return
        base = kind_directory(CONVERSATION_KIND)
        make_directories(
            fs, space, (CELLS_ROOT, base, f"{base}/{cell_id}", self.directory(cell_id))
        )
        self._directories.add((space, cell_id))

    def read(self, ref: RecordRef) -> RecordEntry | None:
        """The entry *ref* names, or None when it cannot be read now."""

        try:
            fs = self._fs()
            version = str(fs.stat(ref.space_id, ref.path).version)
            key = (ref.space_id, ref.path)
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None and cached[0] == version:
                return cached[1].get(ref.key)
            content, read_version = fs.read_text(ref.space_id, ref.path)
            entries = parse_segment(content)
            with self._lock:
                self._cache[key] = (str(read_version), entries)
                self._cache.move_to_end(key)
                while len(self._cache) > READ_CACHE_SEGMENTS:
                    self._cache.popitem(last=False)
            return entries.get(ref.key)
        except Exception:  # noqa: BLE001 - an unreadable record keeps the residue
            return None


__all__ = [
    "ENTRY_MAX_BYTES",
    "READ_CACHE_SEGMENTS",
    "SEGMENT_MAX_BYTES",
    "SEGMENT_MAX_ENTRIES",
    "Recorded",
    "SegmentStore",
    "append_entries",
    "make_directories",
    "writer_name",
    "writer_segment",
]
