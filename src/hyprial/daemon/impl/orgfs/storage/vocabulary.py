# Decision record (phase D, package S, 2026-11-25; design §9 R19):
# - ``StoreError`` and ``document_file_stem`` outlived the journal store they
#   were defined in: the state store, its layout and the space command loop
#   raise or restore ``StoreError``, and the v3 layout names content files
#   by ``document_file_stem``.  They moved here, unchanged, when
#   ``storage/store`` was deleted.  ``src/hyprial/error/`` will collect
#   exception types later; that move is a separate change.
# - The file stem is a fixed-length one-way hash of the document id, so a
#   document id never becomes a path component.
# - Package F6 (R3-8): ``StoreError.message`` keeps the explanation without
#   the ``code: `` prefix ``str()`` adds, so a re-raise does not repeat it.
"""Names shared by the OrgFS stores: the store error and the document file stem."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

DOCUMENT_FILE_PREFIX = "h-"


class StoreError(RuntimeError):
    """A durable store operation was rejected."""

    def __init__(self, code: str, message: str | None = None, **details: Any) -> None:
        self.code = code
        self.details = details
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


def document_file_stem(doc_id: str) -> str:
    """Return the fixed-length on-disk name for ``doc_id`` (no suffix)."""

    digest = hashlib.sha256(doc_id.encode("utf-8")).digest()
    token = base64.b32encode(digest).decode("ascii").lower()
    return DOCUMENT_FILE_PREFIX + token[:32]


__all__ = ["DOCUMENT_FILE_PREFIX", "StoreError", "document_file_stem"]
