"""The OrgFS side of the cell runtime: read, scan and write cell documents.

Decision notes (phase A, S3):

- Only the public ``OrgFs`` protocol is used (``mkdir``, ``listdir``,
  ``read_text_snapshot``, ``document_authors``, ``write_text``).  Every call
  runs on the runtime thread; nothing here is reachable from a lookup.
- A read is trusted only after ``codec.verify`` against the document's OrgFS
  authors, the space owner and the caller's ``writer_of`` (the writer
  bindings of the pass, passed on every call; the mirror holds none).  A
  rejected (space, node, version) is logged once: ``warn``, except
  ``authority-unbound`` (another identity's document, the normal case for
  every foreign cell) at ``info``.
- Writes always carry an expectation: ``expect_version`` of the last copy the
  runtime saw, or ``create_only`` when it knows of none, so a concurrent
  writer surfaces as ``stale-write`` instead of being overwritten.
- An untrusted document comes back as a ``Rejection`` (not as absent) so the
  runtime can tell it from a missing one and block that copy instead of
  writing over it.  Rejections are remembered per (space, node, version) in
  a bounded insertion-ordered map, oldest dropped first;
  ``forget_rejections`` clears it when the writer bindings change.
- Nothing here removes or rewrites a document (contract OC0 D1, §1.10): the
  repair primitive ``remove`` is deleted.  An unadoptable document stays
  byte for byte until an agent moves it by hand (``hyprial fs rm``).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.cells.codec import decode, encode, verify
from hyprial.daemon.impl.cells.model import Cell, CellError
from hyprial.daemon.impl.cells.ports import SpaceRef
from hyprial.daemon.impl.cells.runtime.layout import (
    CELLS_ROOT,
    cell_id_from_filename,
    cell_path,
    kind_directory,
)
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError

_ABSENT_CODES = frozenset({"unknown-doc", "unknown-space", "content_pending", "ORGFS_CONTENT_PENDING"})
_EXISTS_CODES = frozenset({"invalid-argument", "ambiguous-path"})
MAX_REJECTIONS = 4096
WriterOf = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class RemoteCopy:
    """One verified cell document as read from one space."""

    cell: Cell
    space_id: str
    node_id: str
    version: str


@dataclass(frozen=True, slots=True)
class Rejection:
    """A document at a cell's path that readers must not trust."""

    cell_id: str
    space_id: str
    node_id: str
    version: str
    reason: str


ReadResult = RemoteCopy | Rejection


class CellMirror:
    """Stateless OrgFS reader/writer for cell documents."""

    def __init__(self, fs: OrgFs, *, logger: Callable[..., None] | None = None) -> None:
        self._fs = fs
        self._logger = logger
        self._rejected: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._directories: set[tuple[str, str]] = set()

    def read(
        self, space: SpaceRef, kind: str, cell_id: str, writer_of: WriterOf
    ) -> ReadResult | None:
        """The copy of one cell at its path: verified, rejected, or None when absent."""

        return self._read_path(space, kind, cell_path(kind, cell_id), cell_id, writer_of)

    def scan(
        self,
        space: SpaceRef,
        kind: str,
        writer_of: WriterOf,
        unchanged: Callable[[str, str], bool] = lambda _cell_id, _version: False,
    ) -> tuple[ReadResult, ...]:
        """Every *kind* document in *space* whose (id, version) is not *unchanged*."""

        try:
            entries = self._fs.listdir(space.space_id, kind_directory(kind))
        except OrgFsError as error:
            if error.code in _ABSENT_CODES:
                return ()
            raise
        copies: list[ReadResult] = []
        for entry in entries:
            if entry.kind != "doc" or entry.deleted:
                continue
            cell_id = cell_id_from_filename(entry.name)
            if cell_id is None or unchanged(cell_id, str(entry.version)):
                continue
            copy = self._read_path(space, kind, f"id:{entry.node_id}", cell_id, writer_of)
            if copy is not None:
                copies.append(copy)
        return tuple(copies)

    def write(self, space: SpaceRef, cell: Cell, expect_version: str | None) -> tuple[str, str]:
        """Write *cell* to *space*; returns (node_id, version).  Raises OrgFsError."""

        self._ensure_directory(space.space_id, cell.kind)
        options: dict[str, Any] = (
            {"create_only": True} if expect_version is None else {"expect_version": expect_version}
        )
        node = self._fs.write_text(
            space.space_id, cell_path(cell.kind, cell.cell_id), encode(cell), **options
        )
        return node.node_id, str(node.version)

    def forget_rejections(self) -> None:
        """Drop every remembered rejection (the writer bindings changed)."""

        self._rejected.clear()

    def _ensure_directory(self, space_id: str, kind: str) -> None:
        if (space_id, kind) in self._directories:
            return
        for path in (CELLS_ROOT, kind_directory(kind)):
            try:
                self._fs.mkdir(space_id, path)
            except OrgFsError as error:
                if error.code not in _EXISTS_CODES:
                    raise
        self._directories.add((space_id, kind))

    def _read_path(
        self, space: SpaceRef, kind: str, path: str, cell_id: str, writer_of: WriterOf
    ) -> ReadResult | None:
        try:
            snapshot = self._fs.read_text_snapshot(space.space_id, path)
        except OrgFsError as error:
            if error.code in _ABSENT_CODES:
                return None
            raise
        version = str(snapshot.version)
        key = (space.space_id, snapshot.node.node_id, version)
        known = self._rejected.get(key)
        if known is not None:
            return Rejection(cell_id, *key, known)
        try:
            cell = decode(snapshot.content)
            authors = tuple(self._fs.document_authors(space.space_id, path))
        except CellError as error:
            return self._reject(cell_id, key, error.code)
        reason = verify(
            cell,
            authors=authors,
            space_owner=space.owner,
            writer_of=writer_of,
            kind=kind,
            cell_id=cell_id,
            space_role=space.role,
            space_org=space.org,
        )
        if reason is not None:
            return self._reject(cell_id, key, reason)
        return RemoteCopy(cell, space.space_id, snapshot.node.node_id, version)

    def _reject(self, cell_id: str, key: tuple[str, str, str], reason: str) -> Rejection:
        self._rejected[key] = reason
        while len(self._rejected) > MAX_REJECTIONS:
            self._rejected.popitem(last=False)
        rejection = Rejection(cell_id, *key, reason)
        if self._logger is None:
            return rejection
        try:
            self._logger(
                "info" if reason == "authority-unbound" else "warn",
                "cells", "cell.document_rejected",
                space=key[0], node=key[1], version=key[2], reason=reason,
            )
        except Exception:  # noqa: BLE001 - logging never breaks a read
            pass
        return rejection


__all__ = ["CellMirror", "MAX_REJECTIONS", "ReadResult", "Rejection", "RemoteCopy", "WriterOf"]
