"""``ConversationDocuments``: reading conversation records as documents (plan slice RM1).

Decision notes (plan §2.1, §11.1, R13, R15; brief RM1):

- A conversation document is the union of every segment under
  ``cells/conversation/<c.id>/rec-<writer>/`` in every owner-home space of
  the local authority (``CellSpaces.home_spaces``: readers take the union
  of an owner's homes).  Reading never creates a space and never composes
  the cell runtime or OrgFS: ``fs`` answers the facade of an OrgFS that is
  already up, or raises.
- ``cell_entries`` is the history read: one recursive ``listdir`` of the
  conversation's record directory per home space, then ``read_text`` only
  for segments whose node version changed since the last read (segments
  only grow, and a rolled-over segment never changes again).  Parsed
  segments with bodies are kept in a small LRU (``FULL_CACHE_SEGMENTS``).
- ``known_cells`` is the list read and must not scan OrgFS on every call.
  Conversations are enumerated from two sources:
  1. the recorder's own index ``conversation-records.sqlite3`` (opened
     read-only): ``COUNT(*)`` and ``MAX(rowid)`` of the recorded rows per
     conversation is a change marker for everything this node wrote; a
     conversation whose marker moved gets one recursive ``listdir`` of its
     own directory;
  2. a sweep, at most every ``SWEEP_S`` seconds: one recursive ``listdir``
     of ``cells/conversation`` per home space.  It finds conversations and
     segments other writers (another device of the owner, when the home
     space syncs) added, which no local marker shows.
  A warm list therefore costs one local SQLite aggregate and no OrgFS call
  unless a local write moved a marker; a cold list, and one list per
  sweep period, costs one recursive listing per home space plus one
  ``read_text`` per changed segment.  The list keeps entries without
  bodies (heads need no text).
- A conversation directory or segment that cannot be found
  (``unknown-doc``, pending content) reads as empty, so a conversation
  with no record is an empty document, not a failure; a segment read that
  way is not cached, so it is read again on the next refresh even when its
  node version did not move.
  Any other OrgFS failure is ``RecordUnavailable``: the caller answers from
  the delivery queue and says so.  A broken index file only costs
  freshness until the next sweep.
- Entries are kept only when their ``cellId`` names the directory they were
  found in.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.runtime.layout import kind_directory
from hyprial.daemon.impl.conversations.record.contracts import (
    CONVERSATION_KIND,
    RecordEntry,
    RecordUnavailable,
    parse_segment,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError

SWEEP_S = 30.0
FULL_CACHE_SEGMENTS = 32
INDEX_TIMEOUT_S = 1.0
_ABSENT_CODES = frozenset({"unknown-doc", "content_pending", "ORGFS_CONTENT_PENDING"})
_SEGMENT_SUFFIX = ".jsonl"
_WRITER_PREFIX = "rec-"

SegmentKey = tuple[str, str]
Spaces = Callable[[Any], Sequence[str]]


def _segment_cell(path: str, base: str) -> str | None:
    """The ``c.`` id naming the record directory of a segment path, or None."""

    prefix = base + "/"
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split("/")
    if len(parts) != 3:
        return None
    cell_id, writer, name = parts
    if not writer.startswith(_WRITER_PREFIX) or not name.endswith(_SEGMENT_SUFFIX):
        return None
    return cell_id


class ConversationDocuments:
    """Reads a conversation's record segments as one document per id."""

    def __init__(
        self,
        *,
        fs: Callable[[], Any],
        spaces: Spaces,
        index: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        sweep_s: float = SWEEP_S,
    ) -> None:
        self._fs = fs
        self._spaces = spaces
        self._index = None if index is None else Path(index)
        self._clock = clock
        self._sweep_s = sweep_s
        self._base = kind_directory(CONVERSATION_KIND)
        self._lock = threading.Lock()
        self._light: dict[SegmentKey, tuple[str, tuple[RecordEntry, ...]]] = {}
        self._full: OrderedDict[SegmentKey, tuple[str, tuple[RecordEntry, ...]]] = (
            OrderedDict()
        )
        self._cells: dict[str, set[SegmentKey]] = {}
        self._markers: dict[str, tuple[int, int]] = {}
        self._swept_at: float | None = None
        self.reads = 0
        self.listings = 0

    def cell_entries(self, cell_id: str) -> tuple[RecordEntry, ...]:
        """Every entry of one conversation, bodies included; raises ``RecordUnavailable``."""

        with self._lock:
            fs, spaces = self._open()
            found = self._list_cell(fs, spaces, cell_id)
            entries: list[RecordEntry] = []
            for (space, path), version in sorted(found.items()):
                entries.extend(self._full_segment(fs, space, path, version) or ())
            return tuple(entry for entry in entries if entry.cell_id == cell_id)

    def known_cells(self) -> dict[str, tuple[RecordEntry, ...]]:
        """Every known conversation's entries without bodies; raises ``RecordUnavailable``."""

        with self._lock:
            fs, spaces = self._open()
            markers = self._index_markers()
            now = self._clock()
            if self._swept_at is None or now - self._swept_at >= self._sweep_s:
                self._sweep(fs, spaces)
                self._swept_at = now
            else:
                for cell_id, marker in markers.items():
                    if self._markers.get(cell_id) != marker:
                        self._list_cell(fs, spaces, cell_id)
            self._markers = markers
            return {
                cell_id: tuple(
                    entry
                    for key in sorted(keys)
                    for entry in self._light.get(key, ("", ()))[1]
                    if entry.cell_id == cell_id
                )
                for cell_id, keys in self._cells.items()
            }

    def _open(self) -> tuple[Any, tuple[str, ...]]:
        try:
            fs = self._fs()
            spaces = tuple(self._spaces(fs))
        except RecordUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - any OrgFS failure falls back to the queue
            raise RecordUnavailable(str(getattr(error, "code", type(error).__name__))) from error
        return fs, spaces

    def _listing(self, fs: Any, space: str, path: str) -> tuple[Any, ...]:
        self.listings += 1
        try:
            return tuple(fs.listdir(space, path, recursive=True))
        except OrgFsError as error:
            if error.code in _ABSENT_CODES:
                return ()
            raise RecordUnavailable(str(error.code)) from error
        except Exception as error:  # noqa: BLE001 - any OrgFS failure falls back to the queue
            raise RecordUnavailable(type(error).__name__) from error

    def _segments_under(
        self, fs: Any, spaces: Sequence[str], path: str
    ) -> dict[str, dict[SegmentKey, str]]:
        found: dict[str, dict[SegmentKey, str]] = {}
        for space in spaces:
            for node in self._listing(fs, space, path):
                if getattr(node, "kind", None) != "doc":
                    continue
                cell_id = _segment_cell(str(node.path), self._base)
                if cell_id is not None:
                    found.setdefault(cell_id, {})[(space, str(node.path))] = str(node.version)
        return found

    def _list_cell(
        self, fs: Any, spaces: Sequence[str], cell_id: str
    ) -> dict[SegmentKey, str]:
        found = self._segments_under(fs, spaces, f"{self._base}/{cell_id}").get(cell_id, {})
        self._remember(fs, cell_id, found)
        return found

    def _sweep(self, fs: Any, spaces: Sequence[str]) -> None:
        found = self._segments_under(fs, spaces, self._base)
        for cell_id in set(self._cells) - set(found):
            self._cells.pop(cell_id, None)
        for cell_id, segments in found.items():
            self._remember(fs, cell_id, segments)

    def _remember(self, fs: Any, cell_id: str, segments: dict[SegmentKey, str]) -> None:
        for key in self._cells.get(cell_id, set()) - set(segments):
            self._light.pop(key, None)
        for (space, path), version in segments.items():
            known = self._light.get((space, path))
            if known is None or known[0] != version:
                entries = self._full_segment(fs, space, path, version)
                if entries is None:
                    continue
                self._light[(space, path)] = (
                    version,
                    tuple(dataclasses.replace(entry, body=b"") for entry in entries),
                )
        if segments:
            self._cells[cell_id] = set(segments)
        else:
            self._cells.pop(cell_id, None)

    def _full_segment(
        self, fs: Any, space: str, path: str, version: str
    ) -> tuple[RecordEntry, ...] | None:
        key = (space, path)
        cached = self._full.get(key)
        if cached is not None and cached[0] == version:
            self._full.move_to_end(key)
            return cached[1]
        self.reads += 1
        try:
            content, read_version = fs.read_text(space, path)
        except OrgFsError as error:
            if error.code in _ABSENT_CODES:
                return None
            raise RecordUnavailable(str(error.code)) from error
        except Exception as error:  # noqa: BLE001 - any OrgFS failure falls back to the queue
            raise RecordUnavailable(type(error).__name__) from error
        entries = tuple(parse_segment(str(content)).values())
        self._full[key] = (str(read_version), entries)
        self._full.move_to_end(key)
        while len(self._full) > FULL_CACHE_SEGMENTS:
            self._full.popitem(last=False)
        return entries

    def _index_markers(self) -> dict[str, tuple[int, int]]:
        if self._index is None or not self._index.is_file():
            return {}
        try:
            uri = f"{self._index.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=INDEX_TIMEOUT_S)) as db:
                rows = db.execute(
                    "SELECT cell_id, COUNT(*), MAX(rowid) FROM records "
                    "WHERE cell_id IS NOT NULL GROUP BY cell_id"
                ).fetchall()
        except sqlite3.Error:
            return dict(self._markers)
        return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}


__all__ = ["FULL_CACHE_SEGMENTS", "SWEEP_S", "ConversationDocuments"]
