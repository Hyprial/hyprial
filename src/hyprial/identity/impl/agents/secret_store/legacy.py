"""One-time, verified transfer of granted legacy files into AgentSecretStore.

No file is a resolver fallback. Commit SQLite first, verify under a write lock,
then unlink the source. A crash before unlink is retried without replacing rows.
Native harness credentials and ungranted files are never traversed or changed.
"""

from __future__ import annotations

import errno
import hmac
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from typing import TYPE_CHECKING

from hyprial.kernel import private_metadata_violation

if TYPE_CHECKING:
    from .store import AgentSecretStore

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_BYTES = 1024 * 1024


def migrate_entry(
    store: AgentSecretStore,
    entry_id: str,
    *,
    required_fields: Iterable[str],
    repoint: Callable[[dict[str, str], str], None],
) -> str | None:
    """Import a complete source, durably rebind its grants, then delete it.

    Return the field containing the exact text. Its name depends only on the
    source, never on which grant triggered migration. Retries therefore use the
    same representation even after grants have been rebound.
    """

    if not _SEGMENT.fullmatch(entry_id):
        raise ValueError("legacy entry is not a supported file name")
    if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
        raise ValueError("safe legacy migration is unsupported")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    home_fd = os.open(store.agent_home, flags)
    directory_fd = None
    try:
        _private(store.agent_home, os.fstat(home_fd), directory=True)
        try:
            directory_fd = os.open("secrets", flags, dir_fd=home_fd)
        except FileNotFoundError:
            return
        directory = store.agent_home / "secrets"
        directory_stat = os.fstat(directory_fd)
        _private(directory, directory_stat, directory=True)
        try:
            descriptor = os.open(
                entry_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd,
            )
        except FileNotFoundError:
            _remove_empty_directory(home_fd, directory_stat)
            return
        if entry_id == "native" and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return  # Only the native DIRECTORY is third-party harness state.
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            _private(directory / entry_id, metadata, directory=False)
            content = stream.read(_MAX_BYTES + 1)
            if len(content) > _MAX_BYTES:
                raise ValueError("legacy entry exceeds migration size limit")
            values, raw_field = _values(content)
            fields = tuple(required_fields)
            if any(not values.get(field) for field in fields):
                raise ValueError("legacy grant selects an invalid field")
            if raw_field in fields:
                # Only a committed representation can authorize the generated
                # raw field. A previously invalid JSON selector must not gain
                # access to the whole file merely by matching its generated key.
                with closing(store._connect()) as connection:
                    if not _matches(connection, entry_id, values):
                        raise ValueError("legacy grant selects an invalid field")
            _import(store, entry_id, values)
            # Hold SQLite's writer lock while verifying and deleting. A concurrent
            # rotate/remove cannot invalidate verification before source deletion.
            with closing(store._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                if not _matches(connection, entry_id, values):
                    raise ValueError("legacy migration conflicts with SQLite authority")
                current = os.stat(entry_id, dir_fd=directory_fd, follow_symlinks=False)
                stream.seek(0)
                if (
                    _identity(current) != _identity(metadata)
                    or not hmac.compare_digest(stream.read(_MAX_BYTES + 1), content)
                ):
                    raise ValueError("legacy source changed during migration")
                # This commits in the registry before unlink, not after it.
                # Interrupted imports/rebindings are idempotent on restart.
                repoint(values, raw_field)
                os.unlink(entry_id, dir_fd=directory_fd)
                os.fsync(directory_fd)
        _remove_empty_directory(home_fd, directory_stat)
        return raw_field
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(home_fd)


def _private(path, metadata, *, directory: bool) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode) or private_metadata_violation(
        path, metadata, mode=0o700 if directory else 0o600,
    ) is not None:
        raise ValueError("legacy migration requires private regular files and directories")


def _identity(metadata) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _remove_empty_directory(home_fd: int, expected) -> None:
    current = os.stat("secrets", dir_fd=home_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("legacy directory changed during migration")
    try:
        os.rmdir("secrets", dir_fd=home_fd)
    except OSError as error:
        if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
            raise
    else:
        os.fsync(home_fd)


def _values(content: bytes) -> tuple[dict[str, str], str]:
    text = content.decode("utf-8")
    if not text:
        raise ValueError("legacy entry is empty")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        document = None
    values = {
        key: value for key, value in (document.items() if isinstance(document, dict) else ())
        if _SEGMENT.fullmatch(key) and isinstance(value, str)
    }
    # Keep raw text (including whitespace) as well as every selectable field.
    # Do not let a literal VALUE key collide with the fieldless interpretation.
    raw_field = "VALUE"
    index = 0
    while isinstance(document, dict) and raw_field in document:
        index += 1
        raw_field = f"LEGACY_RAW_{index}"
    values[raw_field] = text
    return values, raw_field


def _matches(connection, service: str, values: dict[str, str]) -> bool:
    rows = dict(connection.execute(
        "SELECT field, value FROM secrets WHERE service = ?", (service,),
    ).fetchall())
    return rows.keys() == values.keys() and all(
        hmac.compare_digest(rows[key].encode("utf-8"), value.encode("utf-8"))
        for key, value in values.items()
    )


def _import(store: AgentSecretStore, service: str, values: dict[str, str]) -> None:
    at_ms = time.time_ns() // 1_000_000
    fingerprints = {
        field: store._fingerprint_for_write(
            field, value, actor="system", via="daemon", at_ms=at_ms,
        )
        for field, value in values.items()
    }
    with closing(store._connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        managed = connection.execute(
            """
            SELECT 1 FROM secrets WHERE service = ?
            UNION ALL SELECT 1 FROM secret_events
            WHERE service = ? AND action IN ('set', 'rotate', 'remove', 'migrate') LIMIT 1
            """, (service, service),
        ).fetchone()
        if managed is not None:
            if not _matches(connection, service, values):
                raise ValueError("legacy migration conflicts with SQLite authority")
            return
        for field, value in values.items():
            fingerprint = fingerprints[field]
            connection.execute(
                "INSERT INTO secrets(service, field, value, fingerprint, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?)", (service, field, value, fingerprint, at_ms),
            )
            store._insert_event(
                connection, at_ms=at_ms, actor="system", via="daemon", action="migrate",
                service=service, field=field, fingerprint=fingerprint, result="ok",
            )
        if not _matches(connection, service, values):
            raise ValueError("legacy migration verification failed")
