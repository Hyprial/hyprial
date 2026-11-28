# Decision record (phase D, package D2, 2026-11-25):
# - Plan §1.2: the state runtime's store registry keeps the method names of
#   the legacy ``_StoreRegistry`` that the reused mixins call
#   (``snapshot_ids``, ``snapshot``, ``detach``, ``clear``, ``in``), but it
#   never opens a store on lookup: stores are opened only by the facade's
#   store factory, which this registry records.  ``get`` returns ``None`` for
#   a space without an open store.
# - The facade closes the stores it opened (``detach_space``,
#   ``close_effects``); ``FileStateStore.close`` is idempotent, so the
#   runtime's own close and the local space purge may close them again.
# - Design §2.7 / C1 report §12: the startup scan only reads.  A directory
#   that is not named by a UUID raises ``ORGFS_INVALID_SPACE_DIRECTORY`` as
#   before; a space being purged is skipped as before; ``legacy`` and
#   ``invalid`` directories are refused space by space with
#   ``ORGFS_LAYOUT_UNSUPPORTED`` and are never written; ``v3`` opens.
# - Design §9 R6: an ``uninitialized`` directory (no ``space.json``) and a
#   ``v3`` directory whose meta has no ``space`` root are creations or joins
#   that never completed and were never confirmed to a caller; the runtime
#   deletes them through the local space purge's deletion fence.
# - F1 item 3: a directory holding ``JOIN_MARKER`` is a join that never
#   reached ``complete_join`` (its meta may already name the space and an
#   owner, so the rule above would open it and skip the member and node-gate
#   checks); it is discarded the same way, whatever else it holds.
"""Store registry and startup classification of the state runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

from hyprial.daemon.impl.orgfs.state import (
    FileStateStore,
    is_space_directory_name,
    space_directory_kind,
)

JOIN_MARKER = "join.pending"
ScanVerdict = Literal["open", "refuse", "discard", "invalid-name", "skip"]


@dataclass(frozen=True, slots=True)
class ScanEntry:
    """One directory under ``spaces/`` and what the startup scan does with it."""

    path: Path
    verdict: ScanVerdict
    reason: str | None = None


class StateStoreRegistry:
    """The open ``FileStateStore`` of every space, keyed by space id."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stores: dict[str, FileStateStore] = {}

    def record(self, space_id: str, store: FileStateStore) -> FileStateStore:
        with self._lock:
            self._stores[space_id] = store
        return store

    def get(self, space_id: str, default: object = None) -> FileStateStore | None:
        with self._lock:
            return self._stores.get(space_id)  # type: ignore[return-value]

    def __contains__(self, space_id: object) -> bool:
        with self._lock:
            return space_id in self._stores

    def snapshot_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._stores)

    def snapshot(self) -> tuple[FileStateStore, ...]:
        with self._lock:
            return tuple(self._stores.values())

    def detach(self, space_id: str) -> FileStateStore | None:
        with self._lock:
            return self._stores.pop(space_id, None)

    def clear(self) -> None:
        with self._lock:
            self._stores.clear()


def classify_spaces(spaces_root: Path, purging: set[str]) -> tuple[ScanEntry, ...]:
    """Classify every directory under ``spaces_root`` without changing any."""

    if not spaces_root.is_dir():
        return ()
    entries: list[ScanEntry] = []
    for path in sorted(spaces_root.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        if path.name in purging:
            entries.append(ScanEntry(path, "skip"))
            continue
        if not is_space_directory_name(path.name):
            entries.append(ScanEntry(path, "invalid-name"))
            continue
        if (path / JOIN_MARKER).exists():
            entries.append(ScanEntry(path, "discard", "join-in-progress"))
            continue
        kind = space_directory_kind(path)
        if kind == "v3":
            entries.append(ScanEntry(path, "open"))
        elif kind == "legacy":
            entries.append(ScanEntry(path, "refuse", "legacy-journal"))
        elif kind == "invalid":
            entries.append(ScanEntry(path, "refuse", "unknown-space-json"))
        else:
            entries.append(ScanEntry(path, "discard", "uninitialized"))
    return tuple(entries)


__all__ = ["JOIN_MARKER", "ScanEntry", "ScanVerdict", "StateStoreRegistry", "classify_spaces"]
