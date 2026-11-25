# Decision record (C1 state store, 2026-11-25):
# - Paths follow design section 2.1: ``space.json``, ``batch.pending``,
#   ``meta.{state,delta}`` and ``tree.{state,delta}`` at the space root, content
#   documents under ``docs/<stem>.{state,delta}`` with the stem from the existing
#   ``document_file_stem`` (reused from the old store's vocabulary, not copied;
#   stage E has to move it when ``storage/store`` is deleted).
# - Temporary files have fixed names (``<file>.tmp``) so a leftover is found by
#   name: ``batch.pending.tmp`` is removed when the space is opened, a state temp
#   is overwritten by the next compaction of that document.  Nothing at open time
#   lists ``docs/``, so opening costs the same for any number of documents.
# - The space directory name rule is the existing one at the raise site of
#   ``ORGFS_INVALID_SPACE_DIRECTORY`` (``runtime.py``): ``uuid.UUID(name)`` must
#   accept the name.  The caller keeps raising that code; this module only offers
#   the predicate and the classification of a directory.
# - ``space.json`` is the commit point of space creation: the directory and
#   ``docs/`` are created first, ``space.json`` is renamed into place last.  A
#   directory without it and without ``journal.sqlite3`` is ``uninitialized`` and
#   may be created again.  ``journal.sqlite3`` without a v3 ``space.json`` is the
#   old layout (section 2.7).  A ``space.json`` that is not a v3 object for this
#   space is refused with the same ``ORGFS_LAYOUT_UNSUPPORTED`` and a ``reason``;
#   the design names no other code for it (see the C1 report, design issues).
"""On-disk layout of one OrgFS space (design 2.1, 2.7)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from hyprial.daemon.impl.orgfs.state.interface import (
    DIRECTORY_DOC_IDS,
    LAYOUT_VERSION,
    MIGRATION_GUIDE,
    ORGFS_LAYOUT_UNSUPPORTED,
)
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import StoreError, document_file_stem

SpaceDirectoryKind = Literal["v3", "legacy", "invalid", "uninitialized"]

SPACE_FILE = "space.json"
LEGACY_JOURNAL = "journal.sqlite3"
PENDING_FILE = "batch.pending"
DOCS_DIR = "docs"
TEMP_SUFFIX = ".tmp"


def is_space_directory_name(name: str) -> bool:
    """Whether ``name`` passes the existing space-directory rule."""

    try:
        uuid.UUID(name)
    except ValueError:
        return False
    return True


def read_space_json(space_dir: Path) -> Any:
    """Parsed ``space.json``; ``None`` when absent, ``...`` when not JSON."""

    try:
        raw = (space_dir / SPACE_FILE).read_bytes()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ...


def _is_v3(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("layoutVersion")) is int
        and value["layoutVersion"] == LAYOUT_VERSION
    )


def space_directory_kind(space_dir: Path) -> SpaceDirectoryKind:
    """Classify a directory under ``spaces/`` without changing it."""

    value = read_space_json(space_dir)
    if _is_v3(value):
        return "v3"
    if (space_dir / LEGACY_JOURNAL).exists():
        return "legacy"
    if value is not None:
        return "invalid"
    return "uninitialized"


def unsupported(space_id: str, reason: str) -> StoreError:
    return StoreError(
        ORGFS_LAYOUT_UNSUPPORTED,
        f"space {space_id} is not stored in OrgFS layout {LAYOUT_VERSION} ({reason}); "
        f"see {MIGRATION_GUIDE}",
        spaceId=space_id,
        reason=reason,
        guide=MIGRATION_GUIDE,
    )


def check_space_json(space_dir: Path, space_id: str) -> None:
    kind = space_directory_kind(space_dir)
    if kind == "legacy":
        raise unsupported(space_id, "legacy-journal")
    if kind == "invalid":
        raise unsupported(space_id, "unknown-space-json")
    if kind == "uninitialized":
        raise FileNotFoundError(space_dir / SPACE_FILE)
    value = read_space_json(space_dir)
    if value.get("spaceId") != space_id:
        raise unsupported(space_id, "space-id-mismatch")


def space_json_bytes(space_id: str) -> bytes:
    created = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    value = {"layoutVersion": LAYOUT_VERSION, "spaceId": space_id, "createdAt": created}
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def document_paths(space_dir: Path, doc_id: str) -> tuple[Path, Path]:
    """``(state, delta)`` paths of ``doc_id``."""

    if doc_id in DIRECTORY_DOC_IDS:
        base = space_dir / doc_id
    else:
        base = space_dir / DOCS_DIR / document_file_stem(doc_id)
    return base.with_name(base.name + ".state"), base.with_name(base.name + ".delta")


def temp_path(path: Path) -> Path:
    return path.with_name(path.name + TEMP_SUFFIX)
