"""The group document in the owner's chat space (slice GC1).

Decision notes (group chat design draft §2.2, owner decisions §10 8.1,
2026-11-18):

- A group is a document in the owner's existing chat space (today the
  owner-home space), not a space per group.  Where it lives is behind the
  ``GroupPlacement`` port, so a later cross-owner group can resolve to a
  multi-owner chat space without changing the document format.  The one
  implementation today is ``OwnerHomeGroupPlacement`` in
  ``composition.conversations.placement``: reads take the union of the
  owner's home spaces and never create one, writes go to the lowest id.
- Layout ``conversations/<g.id>/log-<writer>/<seq>.jsonl``, outside
  ``cells/``, so the conversation record readers never see it; the group is
  not a conversation cell.  ``writer`` is this daemon's node id.
- Appends use the record's own append discipline
  (``record.segments.append_entries``: skip keys already in the segment,
  ``expect_version`` / ``create_only``, read back, roll over).  That skip
  only sees one segment, so an append first reads the whole document (every
  writer, every home) and drops entry ids already present: appending the
  same ``entryId`` twice is a no-op wherever the first copy landed.  The
  tail segment is found by listing this writer's directory; there is no
  local index.
- An entry whose own line would not parse back to it is refused with
  ``ValueError`` before anything is written: such a line would stay in the
  shared document unseen by readers and be written again on every retry.
- Every OrgFS failure is ``RecordUnavailable``; a missing directory or
  segment reads as empty.  Reads are not cached this round.
- Appends are serialised by one process-wide lock (E2E-031 GC-11,
  2026-11-20).  Only this daemon writes its ``log-<writer>`` segments, but a
  ``GroupLog`` is built per request, so two members replying at once read
  the same tail version and the second ``expect_version`` write failed as
  ``stale-write``, surfacing as ``DAEMON_UNAVAILABLE`` to the harness.

Decision notes (slice GC2):

- ``groups`` names the groups the readable homes hold a directory for, by
  one listing of ``conversations/`` per home; there is no index.  A
  directory is not yet a group: a reader folds its entries and drops one
  without a ``created`` entry.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from typing import Any, Protocol

from hyprial.daemon.impl.conversations.groups.model import GroupEntry, ordered
from hyprial.daemon.impl.conversations.record.contracts import RecordUnavailable
from hyprial.daemon.impl.conversations.record.segments import (
    append_entries,
    make_directories,
    writer_name,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import ORGFS_CONTENT_PENDING, is_group_id_segment

GROUPS_ROOT = "conversations"
WRITER_PREFIX = "log-"
_ABSENT_CODES = frozenset({"unknown-doc", ORGFS_CONTENT_PENDING})
_SEGMENT = re.compile(r"(\d{6})\.jsonl")
_APPEND_LOCK = threading.Lock()


class GroupPlacement(Protocol):
    """Where a group document is read from and written to."""

    def fs(self) -> Any: ...

    def write_space(self) -> str | None: ...

    def read_fs(self) -> Any: ...

    def read_spaces(self, fs: Any) -> Sequence[str]: ...


def parse_log(content: str) -> dict[str, GroupEntry]:
    """Every well-formed entry of one segment by entry id; the first line wins."""

    entries: dict[str, GroupEntry] = {}
    for line in content.splitlines():
        entry = GroupEntry.from_line(line) if line else None
        if entry is not None:
            entries.setdefault(entry.entry_id, entry)
    return entries


def _require_group(group_id: str) -> None:
    if not isinstance(group_id, str) or not is_group_id_segment(group_id):
        raise ValueError(f"not a group conversation id: {group_id!r}")


def _unavailable(error: Exception) -> RecordUnavailable:
    return RecordUnavailable(str(getattr(error, "code", type(error).__name__)))


class GroupLog:
    """Appends to and reads group documents over a ``GroupPlacement``."""

    def __init__(self, *, placement: GroupPlacement, writer: str) -> None:
        self._placement = placement
        self._writer = WRITER_PREFIX + writer_name(writer)

    def directory(self, group_id: str) -> str:
        return f"{GROUPS_ROOT}/{group_id}/{self._writer}"

    def segment_path(self, group_id: str, seq: int) -> str:
        return f"{self.directory(group_id)}/{seq:06d}.jsonl"

    def read(self, group_id: str) -> tuple[GroupEntry, ...]:
        """The group's entries by ``(createdAtMs, entryId)``; raises ``RecordUnavailable``."""

        _require_group(group_id)
        try:
            fs = self._placement.read_fs()
            spaces = tuple(self._placement.read_spaces(fs))
            return ordered(self._entries(fs, spaces, group_id))
        except RecordUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - any OrgFS failure is an unavailable record
            raise _unavailable(error) from error

    def groups(self) -> tuple[str, ...]:
        """Every group id a readable home has a directory for; raises ``RecordUnavailable``."""

        try:
            fs = self._placement.read_fs()
            found = {
                str(node.name)
                for space in self._placement.read_spaces(fs)
                for node in self._listing(fs, space, GROUPS_ROOT, recursive=False)
                if is_group_id_segment(str(node.name))
            }
        except RecordUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - any OrgFS failure is an unavailable record
            raise _unavailable(error) from error
        return tuple(sorted(found))

    def append(self, group_id: str, entries: Sequence[GroupEntry]) -> dict[str, GroupEntry]:
        """Append *entries*; every one of them as stored, by entry id."""

        _require_group(group_id)
        if any(entry.conversation_id != group_id for entry in entries):
            raise ValueError(f"every entry must belong to {group_id}")
        if any(GroupEntry.from_line(entry.to_line()) != entry for entry in entries):
            raise ValueError("an entry would not read back from its own line")
        try:
            with _APPEND_LOCK:
                return self._append(group_id, entries)
        except RecordUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - any OrgFS failure is an unavailable record
            raise _unavailable(error) from error

    def _append(self, group_id: str, entries: Sequence[GroupEntry]) -> dict[str, GroupEntry]:
        fs = self._placement.fs()
        space = self._placement.write_space()
        if not space:
            raise RecordUnavailable("no-owner-home-space")
        spaces = tuple(dict.fromkeys((*self._placement.read_spaces(fs), space)))
        present = {entry.entry_id: entry for entry in ordered(self._entries(fs, spaces, group_id))}
        fresh: dict[str, GroupEntry] = {}
        for entry in entries:
            if entry.entry_id not in present:
                fresh.setdefault(entry.entry_id, entry)
        if fresh:
            make_directories(
                fs, space, (GROUPS_ROOT, f"{GROUPS_ROOT}/{group_id}", self.directory(group_id))
            )
            stored, _ = append_entries(
                fs,
                space,
                lambda seq: self.segment_path(group_id, seq),
                tuple(fresh.values()),
                self._tail(fs, space, group_id),
                parse_log,
            )
            present.update({key: entry for key, (_path, entry) in stored.items()})
        return {entry.entry_id: present[entry.entry_id] for entry in entries}

    def _tail(self, fs: Any, space: str, group_id: str) -> int:
        seqs = [
            int(match.group(1))
            for node in self._listing(fs, space, self.directory(group_id), recursive=False)
            if (match := _SEGMENT.fullmatch(str(node.name))) is not None
        ]
        return max(seqs, default=0)

    def _entries(self, fs: Any, spaces: Sequence[str], group_id: str) -> list[GroupEntry]:
        base = f"{GROUPS_ROOT}/{group_id}/"
        found: list[GroupEntry] = []
        for space in spaces:
            for node in self._listing(fs, space, base.rstrip("/"), recursive=True):
                path = str(node.path)
                parts = path[len(base):].split("/") if path.startswith(base) else ()
                if (
                    getattr(node, "kind", None) != "doc"
                    or len(parts) != 2
                    or not parts[0].startswith(WRITER_PREFIX)
                    or _SEGMENT.fullmatch(parts[1]) is None
                ):
                    continue
                found.extend(
                    entry
                    for entry in parse_log(self._read(fs, space, path)).values()
                    if entry.conversation_id == group_id
                )
        return found

    @staticmethod
    def _listing(fs: Any, space: str, path: str, *, recursive: bool) -> tuple[Any, ...]:
        try:
            return tuple(fs.listdir(space, path, recursive=recursive))
        except OrgFsError as error:
            if error.code in _ABSENT_CODES:
                return ()
            raise

    @staticmethod
    def _read(fs: Any, space: str, path: str) -> str:
        try:
            content, _version = fs.read_text(space, path)
        except OrgFsError as error:
            if error.code in _ABSENT_CODES:
                return ""
            raise
        return str(content)


__all__ = ["GROUPS_ROOT", "WRITER_PREFIX", "GroupLog", "GroupPlacement", "parse_log"]
