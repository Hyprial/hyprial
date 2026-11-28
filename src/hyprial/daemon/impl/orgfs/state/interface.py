# Decision record (C1 state store, 2026-11-25):
# - Frozen surface of docs/design/orgfs-minimal-design-2026-11-25.md section 2.6:
#   ``Batch`` and the ``StateStore`` Protocol are copied from the design text;
#   the only additions are the compaction thresholds (section 2.3, decision 1 of
#   2026-10-29), the doc ids of the two directory documents, the new error code
#   string and the in-process failure type ``StateStoreBroken``.
# - ``ORGFS_LAYOUT_UNSUPPORTED`` is registered in the frozen wire code table
#   (``hyprial/error/codes.py``, phase D P0) and re-exported from here so the
#   package's callers keep one import site.
# - ``StateStoreBroken`` is not a wire error: it says a commit failed after its
#   batch became durable (or a rollback failed), so the in-process view of the
#   files can no longer be trusted until the store is reopened and recovery runs.
"""Public types of the OrgFS state store (design section 2.6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hyprial.error.codes import ORGFS_LAYOUT_UNSUPPORTED as ORGFS_LAYOUT_UNSUPPORTED

META_DOC_ID = "meta"
TREE_DOC_ID = "tree"
DIRECTORY_DOC_IDS = (META_DOC_ID, TREE_DOC_ID)

COMPACTION_RECORDS = 500
COMPACTION_BYTES = 1024 * 1024

LAYOUT_VERSION = 3
MIGRATION_GUIDE = "docs/guides/migration-orgfs-minimal.md"


@dataclass(frozen=True, slots=True)
class Batch:
    """One atomic write: ordered appends, then permanent content deletions."""

    appends: tuple[tuple[str, bytes], ...] = ()
    deletes: tuple[str, ...] = ()


class StateStoreBroken(RuntimeError):
    """The store must be reopened before it can be used again."""


@runtime_checkable
class StateStore(Protocol):
    """Durable per-document Yjs state of one space."""

    space_id: str
    recovered_batch: bool

    def load(self, doc_id: str) -> bytes | None: ...

    def commit(self, batch: Batch) -> None: ...

    def needs_compaction(self, doc_id: str) -> bool: ...

    def compact(self, doc_id: str, state: bytes | None = None) -> None: ...

    def close(self) -> None: ...
