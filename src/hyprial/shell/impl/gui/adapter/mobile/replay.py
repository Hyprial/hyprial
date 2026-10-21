"""Persistent replay window and the per-call admission boundary.

The nonce insert and the device/grant re-check happen atomically in SQLite.
``admission`` then holds the authority's process/file lock across the caller's
business dispatch, so a concurrent revocation cannot commit between the check
and the side effect.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from hyprial.shell.impl.gui.adapter.mobile.models import (
    NONCE_CAPACITY_PER_DEVICE,
    OPERATION_PERMISSIONS,
    SESSIONLESS_OPERATIONS,
    MobileAccessError,
    _refusal,
)


class ReplayAdmissionMixin:
    """Methods that must run inside one transaction/lock boundary."""

    def consume_nonce(
        self,
        device_id: str,
        generation: int,
        nonce: str,
        ts: int,
        *,
        now: float | None = None,
    ) -> None:
        """Atomically burn ``(device, generation, nonce)`` in the durable DB."""

        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            device = connection.execute(
                "SELECT key_generation, status FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if device is None:
                raise _refusal(
                    "device-unknown",
                    "the device is not known to this host",
                    "unknown-device",
                )
            if str(device[1]) != "active":
                raise _refusal(
                    "device-revoked",
                    "the device authorization was revoked",
                    "device-revoked",
                )
            if int(device[0]) != generation:
                raise _refusal(
                    "auth-device-generation-stale",
                    "the device credential belongs to an older key generation",
                    "stale-device-generation",
                    expectedGeneration=int(device[0]),
                )
            connection.execute(
                "DELETE FROM nonces WHERE expires_at <= ?", (current,)
            )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nonces WHERE device_id = ?", (device_id,)
                ).fetchone()[0]
            )
            if count >= NONCE_CAPACITY_PER_DEVICE:
                raise _refusal(
                    "auth-nonce-window-full",
                    "the replay window is at capacity; refusing new requests",
                    "nonce-window-full",
                    capacity=NONCE_CAPACITY_PER_DEVICE,
                )
            try:
                connection.execute(
                    "INSERT INTO nonces(device_id, generation, nonce, expires_at, "
                    "consumed_at) VALUES(?, ?, ?, ?, ?)",
                    (device_id, generation, nonce, ts + 120.0, current),
                )
            except sqlite3.IntegrityError as error:
                raise _refusal(
                    "auth-nonce-replayed",
                    "this nonce was already used inside the replay window",
                    "replayed-request",
                    deviceId=device_id,
                ) from error

    def check_session_access(
        self,
        device_id: str,
        generation: int,
        *,
        operation: str,
        session_id: str | None,
        author: str,
    ) -> str:
        """Return the sharing label for one admitted operation or refuse."""

        with self._lock:
            return self._check_session_access(
                self._connection(),
                device_id,
                generation,
                operation=operation,
                session_id=session_id,
                author=author,
            )

    def _check_session_access(
        self,
        connection: sqlite3.Connection,
        device_id: str,
        generation: int,
        *,
        operation: str,
        session_id: str | None,
        author: str,
    ) -> str:
        device = connection.execute(
            "SELECT owner, key_generation, status, permissions FROM devices "
            "WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if device is None:
            raise _refusal(
                "device-unknown", "the device is not known", "unknown-device"
            )
        owner, current_generation, status, permissions_raw = device
        if status != "active":
            raise _refusal(
                "device-revoked",
                "the device authorization was revoked",
                "device-revoked",
            )
        if int(current_generation) != generation:
            raise _refusal(
                "auth-device-generation-stale",
                "the device credential belongs to an older key generation",
                "stale-device-generation",
            )
        if str(owner) != author:
            raise _refusal(
                "auth-not-authorized",
                "the author is not the device row's owner",
                "author-device-mismatch",
            )
        if operation in SESSIONLESS_OPERATIONS:
            if session_id is not None:
                raise _refusal(
                    "auth-not-authorized",
                    "a sessionless operation must not carry a session id",
                    "not-authorized",
                )
            return "direct"
        permission = OPERATION_PERMISSIONS.get(operation)
        if permission is None:
            raise _refusal(
                "auth-not-authorized",
                "the operation has no authorization row",
                "not-authorized",
            )
        if session_id is None:
            raise _refusal(
                "auth-not-authorized",
                "the operation needs a session the request never named",
                "not-authorized",
            )
        permissions = set(json.loads(permissions_raw))
        if permission not in permissions:
            raise _refusal(
                "auth-permission-denied",
                "the device was not approved for this operation",
                "permission-denied",
                requiredPermission=permission,
            )
        grant = connection.execute(
            "SELECT owner, shared_from FROM session_grants "
            "WHERE device_id = ? AND session_id = ? AND revoked_at IS NULL",
            (device_id, session_id),
        ).fetchone()
        if grant is None:
            raise _refusal(
                "auth-session-not-granted",
                "the session was not explicitly granted to this device",
                "session-not-granted",
                sessionId=session_id,
            )
        if str(grant[0]) != str(owner):
            raise _refusal(
                "owner-mismatch",
                "the session grant belongs to another owner",
                "owner-mismatch",
            )
        return "shared" if grant[1] else "isolated"

    @contextmanager
    def admission(
        self,
        device_id: str,
        generation: int,
        *,
        operation: str,
        session_id: str | None,
        author: str,
    ) -> Iterator[dict[str, Any]]:
        """Hold the authorization boundary across the caller's side effect.

        The cross-process lock is held from the re-check through the caller's
        dispatch, so ``revoke_device`` cannot commit between them.  A durable
        audit row (``request-admitted``) is written before the side effect.
        """

        with self._lock, self._file_lock():
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._checked_now(connection, None)
                sharing = self._check_session_access(
                    connection,
                    device_id,
                    generation,
                    operation=operation,
                    session_id=session_id,
                    author=author,
                )
                source = None
                if session_id is not None and sharing in {"shared", "isolated"}:
                    row = connection.execute(
                        "SELECT shared_from FROM session_grants WHERE device_id = ? "
                        "AND session_id = ? AND revoked_at IS NULL",
                        (device_id, session_id),
                    ).fetchone()
                    source = None if row is None else row[0]
                cursor = connection.execute(
                    "INSERT INTO access_audit(at, event, device_id, session_id, "
                    "operation, sharing, source_device_id, status) "
                    "VALUES(?, 'request-admitted', ?, ?, ?, ?, ?, 'admitted')",
                    (current, device_id, session_id, operation, sharing, source),
                )
                audit_id = int(cursor.lastrowid)
                sequence = self._bump_sequence(connection)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            self._write_epoch(sequence)
            outcome = "completed"
            try:
                yield {
                    "auditId": audit_id,
                    "deviceId": device_id,
                    "sessionId": session_id,
                    "sharing": sharing,
                    "sourceDeviceId": source,
                }
            except BaseException:
                outcome = "failed"
                raise
            finally:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    ended = self._checked_now(connection, None)
                    connection.execute(
                        "UPDATE access_audit SET status = ?, at = ? WHERE audit_id = ?",
                        (outcome, ended, audit_id),
                    )
                    sequence = self._bump_sequence(connection)
                    connection.execute("COMMIT")
                    self._write_epoch(sequence)
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise

    def connection_allowed(self, device_id: str) -> bool:
        """False when the connection's device is revoked or unreadable."""

        try:
            with self._lock:
                row = self._connection().execute(
                    "SELECT status FROM devices WHERE device_id = ?", (device_id,)
                ).fetchone()
        except (MobileAccessError, sqlite3.Error):
            return False
        return row is not None and row[0] == "active"
