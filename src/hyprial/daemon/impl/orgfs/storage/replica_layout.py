"""On-disk layout of a replica's document directories.

A document's objects live under ``<kind>/<space>/<document_file_stem(id)>``,
never under the id itself: protected directory ids grow with user and device
names and passed the 255-byte path component limit for real Casdoor names.
The id stays recoverable from the directory's ``.docid`` file.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from hyprial.kernel import lock_exclusive, unlock

from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    document_file_stem,
    is_document_file_stem,
)

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._:-]+$")
#: Document ids are key segments but never path components (each document's
#: directory is ``document_file_stem(doc_id)``), so they get the protocol's
#: cap on protected ids instead of the filesystem's component limit.
_MAX_DOCUMENT_KEY_SEGMENT = 1024
#: Inside a document directory: the document id the hashed name stands for.
_DOCUMENT_ID_FILE = ".docid"
_DOCUMENT_KINDS = frozenset({"log", "snapshot"})


def _document_key_segment(value: str, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or value.startswith(".")
        or len(value) > _MAX_DOCUMENT_KEY_SEGMENT
        or _SAFE_SEGMENT.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be a safe key segment")
    return value


class DocumentDirectories:
    """Hashed document directories for ``FsReplicaBackend``.

    The host provides ``root``, ``space_id``, ``_fsync_directory`` and a
    ``_document_ids`` cache.
    """

    root: Path
    space_id: str
    _document_ids: dict[Path, str]

    def _document_directory(self, kind: str, doc_id: str) -> Path:
        return self.root / kind / self.space_id / document_file_stem(doc_id)

    def _write_document_id(self, directory: Path, doc_id: str) -> None:
        """Record ``doc_id`` in its directory before any object lands there."""

        marker = directory / _DOCUMENT_ID_FILE
        try:
            recorded = marker.read_text("ascii")
        except FileNotFoundError:
            recorded = None
        if recorded is not None:
            if recorded != doc_id:
                raise ValueError("replica document directory names another document")
            self._document_ids[directory] = doc_id
            return
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{_DOCUMENT_ID_FILE}.tmp-", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(doc_id.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            self._fsync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)
        self._document_ids[directory] = doc_id

    def _document_id_of(self, directory: Path) -> str:
        cached = self._document_ids.get(directory)
        if cached is not None:
            return cached
        try:
            doc_id = (directory / _DOCUMENT_ID_FILE).read_text("ascii")
        except FileNotFoundError as exc:
            raise ValueError(f"replica document directory {directory.name} has no id") from exc
        _document_key_segment(doc_id, name="doc_id")
        if document_file_stem(doc_id) != directory.name:
            raise ValueError(f"replica document directory {directory.name} has a foreign id")
        self._document_ids[directory] = doc_id
        return doc_id

    def _migrate_document_directories(self) -> None:
        """Rename id-named document directories (0.5.2 and earlier) once.

        The id is written into the old directory first, then the directory is
        renamed to its hashed name and the parent fsynced, so a crash leaves a
        state the next open resumes from.  If both exist the hashed directory
        wins and any object missing there moves over (objects are immutable,
        so equal keys hold equal bytes).  An older build cannot read the new
        layout.
        """

        with (self.root / ".docs-migration.lock").open("a+b") as stream:
            lock_exclusive(stream.fileno())
            try:
                for kind in sorted(_DOCUMENT_KINDS):
                    base = self.root / kind / self.space_id
                    if not base.is_dir():
                        continue
                    for legacy in sorted(base.iterdir()):
                        if not legacy.is_dir() or is_document_file_stem(legacy.name):
                            continue
                        self._migrate_document_directory(base, legacy)
            finally:
                unlock(stream.fileno())

    def _migrate_document_directory(self, base: Path, legacy: Path) -> None:
        doc_id = _document_key_segment(legacy.name, name="doc_id")
        self._write_document_id(legacy, doc_id)
        target = base / document_file_stem(doc_id)
        if not target.exists():
            os.rename(legacy, target)
            self._fsync_directory(base)
            self._document_ids.pop(legacy, None)
            self._document_ids[target] = doc_id
            return
        self._write_document_id(target, doc_id)
        for path in sorted(legacy.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            moved = target / path.relative_to(legacy)
            if not moved.exists():
                moved.parent.mkdir(parents=True, exist_ok=True)
                os.rename(path, moved)
                self._fsync_directory(moved.parent)
        for path in sorted(legacy.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        legacy.rmdir()
        self._fsync_directory(base)
        self._document_ids.pop(legacy, None)
