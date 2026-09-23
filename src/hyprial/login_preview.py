"""Read-only owner-migration rehearsal used by ``login --switch-account --dry-run``.

SQLite inputs are copied with SQLite's online ``backup()`` API into an
owner-only temporary directory.  Classification then runs exclusively over
those copies through :func:`hyprial.daemon.owner_migration.build_plan`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from hyprial.daemon.owner_migration import (
    ARCHIVAL_COLUMNS,
    MIGRATION_DATABASES,
    MIGRATION_TEXT_FILES,
    MigrationPlan,
    OwnerMigrationAborted,
    build_plan,
    detect_previous_owner,
)


class LoginPreviewError(RuntimeError):
    """A bounded, structured rehearsal failure."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass(frozen=True, slots=True)
class SourceStatus:
    name: str
    kind: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "kind": self.kind, "status": self.status}


@dataclass(frozen=True, slots=True)
class SQLiteReadArtifact:
    source: str
    path: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "effect": "created-by-sqlite-read-protocol",
            "bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class LoginMigrationPreview:
    target_owner: str
    settings_owner: str | None
    data_owner: str | None
    classification_owner: str | None
    snapshot_started_at: str
    snapshot_finished_at: str
    sources: tuple[SourceStatus, ...]
    sqlite_read_artifacts: tuple[SQLiteReadArtifact, ...]
    rewritten_cells: int
    changed_files: int
    scanned_tables: int
    scanned_columns: int
    matched_cells: int
    archival_columns: tuple[str, ...]
    skipped_archival_columns: tuple[str, ...]
    unclassified: tuple[dict[str, str], ...]

    @property
    def ready(self) -> bool:
        return not self.unclassified

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "blocked",
            "targetOwner": self.target_owner,
            "settingsOwner": self.settings_owner,
            "dataOwner": self.data_owner,
            "classificationOwner": self.classification_owner,
            "snapshot": {
                "startedAt": self.snapshot_started_at,
                "finishedAt": self.snapshot_finished_at,
            },
            "sources": [source.as_dict() for source in self.sources],
            "sqliteReadArtifacts": [
                artifact.as_dict() for artifact in self.sqlite_read_artifacts
            ],
            "rewrittenCells": self.rewritten_cells,
            "changedFiles": self.changed_files,
            "scan": {
                "tables": self.scanned_tables,
                "columns": self.scanned_columns,
                "matchedCells": self.matched_cells,
                "sourceCount": len(self.sources),
                "unmeasurable": 0,
            },
            "archivalColumns": list(self.archival_columns),
            "skippedArchivalColumns": list(self.skipped_archival_columns),
            "unclassified": list(self.unclassified),
        }


def _settings_owner(home: Path) -> str | None:
    path = home / "settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LoginPreviewError(
            "PREVIEW_SETTINGS_UNREADABLE",
            f"cannot read settings for migration preview: {type(error).__name__}",
            {"source": "settings.json"},
        ) from error
    if not isinstance(value, dict):
        raise LoginPreviewError(
            "PREVIEW_SETTINGS_UNREADABLE",
            "settings for migration preview is not an object",
            {"source": "settings.json"},
        )
    owner = value.get("owner")
    if owner is None:
        return None
    if not isinstance(owner, str) or not owner.strip() or ":" in owner:
        raise LoginPreviewError(
            "PREVIEW_SETTINGS_UNREADABLE",
            "settings owner is invalid for migration preview",
            {"source": "settings.json"},
        )
    return owner.strip()


def _path_identity(root: Path, path: Path) -> tuple[Any, ...]:
    """Bounded identity for one declared migration input.

    SQLite database and WAL *content* may legitimately change under the live
    daemon while ``backup()`` still yields a consistent database snapshot.
    Their inode/type/mode guard catches the preview creating, replacing, or
    deleting an input without falsely attributing another writer's bytes to
    this read-only process.  JSON has no online-backup protocol, so its copied
    bytes stay content-sensitive.
    """

    relative = str(path.relative_to(root))
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return (relative, "absent")
    if path.is_symlink():
        return (relative, "symlink", os.readlink(path))
    if path.is_file():
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.name in MIGRATION_TEXT_FILES
            else "<sqlite-content-owned-by-backup>"
        )
        return (
            relative,
            "file",
            stat.st_dev,
            stat.st_ino,
            stat.st_mode & 0o777,
            digest,
        )
    return (relative, "other", stat.st_mode)


def _migration_input_snapshot(root: Path) -> tuple[tuple[Any, ...], ...]:
    """Inspect exactly six DBs, their WALs, and two JSON migration inputs.

    The rest of a production state tree (logs, daemon receipts, captures,
    sidecar state, and unrelated application data) is intentionally outside
    this proof.  Recursing over it both hashes hundreds of irrelevant MiB and
    makes a live dry-run fail whenever an unrelated writer appends a byte.
    """

    paths = [root / name for name in MIGRATION_DATABASES]
    paths.extend(root / f"{name}-wal" for name in MIGRATION_DATABASES)
    paths.extend(root / name for name in MIGRATION_TEXT_FILES)
    return tuple(_path_identity(root, path) for path in paths)


def _sqlite_shm_snapshot(root: Path) -> tuple[tuple[Any, ...], ...]:
    """Observe SHM only to report tightly bounded SQLite read artifacts."""

    return tuple(
        _path_identity(root, root / f"{name}-shm") for name in MIGRATION_DATABASES
    )


def _snapshot_by_name(
    snapshot: tuple[tuple[Any, ...], ...],
) -> dict[str, tuple[Any, ...]]:
    return {str(item[0]): item for item in snapshot}


def _created_protocol_artifact(
    *,
    root: Path,
    source: str,
    suffix: str,
    before: tuple[Any, ...],
    after: tuple[Any, ...],
) -> SQLiteReadArtifact | None:
    """Recognize only SQLite's bounded first-reader WAL/SHM creation."""

    if before[1:] != ("absent",) or len(after) < 2 or after[1] != "file":
        return None
    path = root / f"{source}{suffix}"
    try:
        size = path.stat().st_size
    except OSError:
        return None
    valid_size = size == 0 if suffix == "-wal" else size >= 32768 and size % 32768 == 0
    if not valid_size:
        return None
    return SQLiteReadArtifact(
        source=source,
        path=f"{source}{suffix}",
        size_bytes=size,
    )


def _validate_source_guard(
    *,
    root: Path,
    before: tuple[tuple[Any, ...], ...],
    after: tuple[tuple[Any, ...], ...],
    shm_before: tuple[tuple[Any, ...], ...],
    shm_after: tuple[tuple[Any, ...], ...],
) -> tuple[SQLiteReadArtifact, ...]:
    """Reject structural input changes except exact SQLite read artifacts."""

    artifacts: list[SQLiteReadArtifact] = []
    before_by_name = _snapshot_by_name(before)
    after_by_name = _snapshot_by_name(after)
    shm_before_by_name = _snapshot_by_name(shm_before)
    shm_after_by_name = _snapshot_by_name(shm_after)
    for path, previous in before_by_name.items():
        current = after_by_name[path]
        if current == previous:
            continue
        artifact = None
        if path.endswith("-wal"):
            artifact = _created_protocol_artifact(
                root=root,
                source=path.removesuffix("-wal"),
                suffix="-wal",
                before=previous,
                after=current,
            )
        if artifact is None:
            raise LoginPreviewError(
                "PREVIEW_SOURCE_CHANGED",
                "a migration input changed structurally while the preview was "
                "being snapshotted; no preview result is a commit licence",
                {"sourceInventoryChanged": True, "changedSource": path},
            )
        artifacts.append(artifact)
    for path, previous in shm_before_by_name.items():
        current = shm_after_by_name[path]
        if current == previous:
            continue
        artifact = _created_protocol_artifact(
            root=root,
            source=path.removesuffix("-shm"),
            suffix="-shm",
            before=previous,
            after=current,
        )
        if artifact is None:
            raise LoginPreviewError(
                "PREVIEW_SOURCE_CHANGED",
                "SQLite shared-memory state changed outside the bounded read "
                "protocol shape",
                {"sourceInventoryChanged": True, "changedSource": path},
            )
        artifacts.append(artifact)
    return tuple(artifacts)


def _backup_database(source: Path, destination: Path) -> None:
    source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_db = sqlite3.connect(destination)
    try:
        source_db.backup(destination_db)
    finally:
        destination_db.close()
        source_db.close()


def _safe_context(sample: str, owner: str) -> str:
    """Keep location utility without disclosing inbox/prose around a hit."""

    index = sample.find(owner)
    if index < 0:
        return f"<redacted:{len(sample)} chars>"
    suffix = len(sample) - index - len(owner)
    return f"<redacted:{index} chars>{owner}<redacted:{suffix} chars>"


def _empty_plan() -> MigrationPlan:
    return MigrationPlan()


def preview_owner_migration(
    *,
    state_dir: Path,
    hyprial_home: Path,
    target_owner: str,
    now: Callable[[], datetime] | None = None,
    cleanup: Callable[[Path], None] = shutil.rmtree,
) -> LoginMigrationPreview:
    """Snapshot and classify without mutating the source home or state.

    Cleanup runs for success, exceptions, and interrupts.  A cleanup failure is
    loud and names only the residual directory, never copied contents.
    """

    state_dir = Path(state_dir)
    home = Path(hyprial_home)
    clock = now or (lambda: datetime.now(UTC))
    started = clock().isoformat()
    temporary = Path(tempfile.mkdtemp(prefix="hyprial-login-preview-"))
    os.chmod(temporary, 0o700)
    copy_root = temporary / "state"
    copy_root.mkdir(mode=0o700)
    pending_error: BaseException | None = None
    result: LoginMigrationPreview | None = None
    try:
        settings_owner = _settings_owner(home)
        before = _migration_input_snapshot(state_dir)
        shm_before = _sqlite_shm_snapshot(state_dir)
        statuses: list[SourceStatus] = []
        for name in MIGRATION_DATABASES:
            source = state_dir / name
            if not source.exists():
                statuses.append(SourceStatus(name, "sqlite", "absent"))
                continue
            try:
                _backup_database(source, copy_root / name)
            except (OSError, sqlite3.Error) as error:
                raise LoginPreviewError(
                    "PREVIEW_SOURCE_UNREADABLE",
                    f"cannot snapshot {name} for migration preview: "
                    f"{type(error).__name__}",
                    {"source": name, "kind": "sqlite"},
                ) from error
            statuses.append(SourceStatus(name, "sqlite", "snapshotted"))
        for name in MIGRATION_TEXT_FILES:
            source = state_dir / name
            if not source.exists():
                statuses.append(SourceStatus(name, "json", "absent"))
                continue
            try:
                content = source.read_bytes()
                json.loads(content)
                (copy_root / name).write_bytes(content)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise LoginPreviewError(
                    "PREVIEW_SOURCE_UNREADABLE",
                    f"cannot snapshot {name} for migration preview: "
                    f"{type(error).__name__}",
                    {"source": name, "kind": "json"},
                ) from error
            statuses.append(SourceStatus(name, "json", "snapshotted"))
        after = _migration_input_snapshot(state_dir)
        shm_after = _sqlite_shm_snapshot(state_dir)
        sqlite_read_artifacts = _validate_source_guard(
            root=state_dir,
            before=before,
            after=after,
            shm_before=shm_before,
            shm_after=shm_after,
        )

        try:
            data_owner = detect_previous_owner(copy_root, target_owner)
        except OwnerMigrationAborted as error:
            raise LoginPreviewError(
                "PREVIEW_MULTIPLE_OWNERS",
                "migration preview found multiple data owners and will not pick one",
                {
                    "owners": sorted(item.sample for item in error.unclassified),
                },
            ) from error
        classification_owner = data_owner or settings_owner
        if classification_owner is None or classification_owner == target_owner:
            plan = _empty_plan()
        else:
            try:
                plan = build_plan(
                    state_dir=copy_root,
                    # The classification residuals are string matches on
                    # VALUES, and the copied databases still spell the REAL
                    # home's path inside their payloads — so the home-root
                    # residual must be built from the real home, not from
                    # this preview's own temporary root, or the preview
                    # would abort where the real migration classifies
                    # cleanly (and vice versa).  build_plan never touches
                    # the home on disk; it only string-matches its path.
                    hyprial_home=home,
                    old=classification_owner,
                    new=target_owner,
                )
            except (OSError, sqlite3.Error, UnicodeError) as error:
                raise LoginPreviewError(
                    "PREVIEW_CLASSIFICATION_FAILED",
                    "migration preview could not classify every copied source",
                    {"errorType": type(error).__name__},
                ) from error
        unclassified = tuple(
            {
                "source": item.source,
                "column": item.column,
                "context": _safe_context(item.sample, classification_owner or ""),
            }
            for item in plan.unclassified
        )
        finished = clock().isoformat()
        result = LoginMigrationPreview(
            target_owner=target_owner,
            settings_owner=settings_owner,
            data_owner=data_owner,
            classification_owner=classification_owner,
            snapshot_started_at=started,
            snapshot_finished_at=finished,
            sources=tuple(statuses),
            sqlite_read_artifacts=sqlite_read_artifacts,
            rewritten_cells=len(plan.writes),
            changed_files=len(plan.files),
            scanned_tables=plan.scanned_tables,
            scanned_columns=plan.scanned_columns,
            matched_cells=plan.matched_cells,
            archival_columns=tuple(f"{table}.{column}" for table, column in ARCHIVAL_COLUMNS),
            skipped_archival_columns=tuple(sorted(set(plan.skipped_archival_columns))),
            unclassified=unclassified,
        )
    except BaseException as error:
        pending_error = error
    try:
        cleanup(temporary)
    except BaseException as cleanup_error:
        raise LoginPreviewError(
            "PREVIEW_CLEANUP_FAILED",
            "migration preview copy could not be removed; inspect and remove the "
            "owner-only residual directory",
            {
                "residualDirectory": str(temporary),
                "cleanupErrorType": type(cleanup_error).__name__,
                "operationErrorType": (
                    type(pending_error).__name__ if pending_error is not None else None
                ),
            },
        ) from cleanup_error
    if pending_error is not None:
        raise pending_error
    assert result is not None
    return result
