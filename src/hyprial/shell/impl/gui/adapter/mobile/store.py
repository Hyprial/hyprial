"""Durable mobile-access authority: pairing, devices, revocation and grants.

The storage primitives (one SQLite file, the transaction/epoch boundary) live
in :mod:`schema`; the replay window and admission boundary live in
:mod:`replay`; the shared vocabulary lives in :mod:`models`.  This module is
the state machine an operator and the host composition root consume.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from typing import Any

from hyprial.shell.impl.gui.adapter.envelope import DeviceRow
from hyprial.shell.impl.gui.adapter.mobile.models import (
    CODE_MAX_ATTEMPTS,
    GLOBAL_FAILURE_LIMIT,
    GLOBAL_FAILURE_WINDOW_SECONDS,
    GLOBAL_LOCKOUT_SECONDS,
    PAIRING_CODE_TTL_SECONDS,
    PAIRING_REQUEST_TTL_SECONDS,
    MobileAccessError,
    _identifier,
    _permissions,
    _public_key,
    _refusal,
    _text,
    new_pairing_code,
    normalize_pairing_code,
    pairing_fingerprint,
)
from hyprial.shell.impl.gui.adapter.mobile.pairing.store_authority import (
    PairingAttemptMixin,
)
from hyprial.shell.impl.gui.adapter.mobile.replay import ReplayAdmissionMixin
from hyprial.shell.impl.gui.adapter.mobile.schema import _Core


class MobileAccessStore(PairingAttemptMixin, ReplayAdmissionMixin, _Core):
    """One host's durable access authority; safe across threads and processes."""

    def issue_pairing_code(
        self, *, operator: str, now: float | None = None
    ) -> tuple[str, float]:
        """Mint one short-lived code; the plaintext exists only in this return."""

        operator = _text(operator, "operator", limit=200)
        code = new_pairing_code()
        salt = secrets.token_hex(16)
        code_hash = hashlib.sha256((salt + code).encode("ascii")).hexdigest()
        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            connection.execute(
                "UPDATE pairing_codes SET active = 0 WHERE active = 1"
            )
            expires_at = current + PAIRING_CODE_TTL_SECONDS
            connection.execute(
                "INSERT INTO pairing_codes(code_hash, salt, created_at, expires_at, "
                "consumed_at, active, attempts) VALUES(?, ?, ?, ?, NULL, 1, 0)",
                (code_hash, salt, current, expires_at),
            )
            self._audit(
                connection,
                current,
                "pairing-code-issued",
                status="active",
                detail={"operator": operator, "expiresAt": expires_at},
            )
        return code, expires_at

    def redeem_pairing_code(
        self,
        *,
        code: object,
        device_id: object,
        device_uid: object,
        name: object,
        owner: object,
        client_public: object,
        permissions: object = None,
        request_ttl_seconds: float = PAIRING_REQUEST_TTL_SECONDS,
        pairing_attempt_id: object = None,
        mode: object = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Redeem a code into one pending request; it grants nothing yet.

        Without ``pairing_attempt_id`` this is the legacy operator path.  The
        S1 state machine always supplies an attempt id; that path binds the
        attempt hash, consumes the code and writes the request in one
        transaction (see :mod:`pairing.store_authority`).
        """

        if pairing_attempt_id is not None:
            return self._redeem_pairing_attempt(
                code=code,
                device_id=device_id,
                device_uid=device_uid,
                name=name,
                owner=owner,
                client_public=client_public,
                permissions=permissions,
                pairing_attempt_id=pairing_attempt_id,
                mode=mode,
                now=now,
                request_ttl_seconds=request_ttl_seconds,
            )

        normalized = normalize_pairing_code(code)
        checked_device = _identifier(device_id, "deviceId")
        checked_uid = _identifier(device_uid, "deviceUid")
        checked_name = _text(name, "name", limit=200)
        checked_owner = _text(owner, "owner")
        checked_public = _public_key(client_public)
        checked_permissions = _permissions(permissions)
        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            self._refuse_locked_pairing(connection, current)
            row = connection.execute(
                "SELECT code_hash, salt, expires_at, consumed_at, attempts, active "
                "FROM pairing_codes ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row is None:
                self._record_pairing_failure(connection, current)
                raise _refusal(
                    "pairing-code-invalid",
                    "the pairing code is missing or does not match",
                    "pairing-code-invalid",
                    commit_state=True,
                )
            code_hash, salt, expires_at, consumed_at, attempts, active = row
            if consumed_at is not None:
                self._record_pairing_failure(connection, current)
                raise _refusal(
                    "pairing-code-consumed",
                    "this pairing code has already been redeemed",
                    "pairing-code-consumed",
                    commit_state=True,
                )
            if not active or expires_at <= current:
                connection.execute(
                    "UPDATE pairing_codes SET active = 0 WHERE code_hash = ?",
                    (code_hash,),
                )
                self._record_pairing_failure(connection, current)
                raise _refusal(
                    "pairing-code-expired",
                    "this pairing code has expired; start pairing again",
                    "pairing-code-expired",
                    commit_state=True,
                )
            expected = hashlib.sha256((salt + normalized).encode("ascii")).hexdigest()
            if not hmac.compare_digest(expected, str(code_hash)):
                updated = int(attempts) + 1
                connection.execute(
                    "UPDATE pairing_codes SET attempts = ?, active = ? "
                    "WHERE code_hash = ?",
                    (updated, 0 if updated >= CODE_MAX_ATTEMPTS else 1, code_hash),
                )
                self._record_pairing_failure(connection, current)
                raise _refusal(
                    "pairing-code-invalid",
                    "the pairing code is missing or does not match",
                    "pairing-code-invalid",
                    commit_state=True,
                )
            connection.execute(
                "UPDATE pairing_codes SET consumed_at = ?, active = 0 "
                "WHERE code_hash = ?",
                (current, code_hash),
            )
            request_id = "pair-" + secrets.token_hex(16)
            fingerprint = pairing_fingerprint(
                node_id=self.node_id,
                device_id=checked_device,
                device_uid=checked_uid,
                name=checked_name,
                owner=checked_owner,
                client_public=checked_public,
                permissions=checked_permissions,
            )
            request_expires = min(
                current + request_ttl_seconds,
                float(expires_at),
            )
            connection.execute(
                "INSERT INTO pairing_requests(request_id, device_id, device_uid, name, "
                "owner, client_public, permissions, fingerprint, node_id, requested_at, "
                "expires_at, status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    request_id,
                    checked_device,
                    checked_uid,
                    checked_name,
                    checked_owner,
                    checked_public,
                    json.dumps(checked_permissions, separators=(",", ":")),
                    fingerprint,
                    self.node_id,
                    current,
                    request_expires,
                ),
            )
            self._clear_pairing_failures(connection)
            self._audit(
                connection,
                current,
                "pairing-requested",
                device_id=checked_device,
                status="pending",
                detail={
                    "requestId": request_id,
                    "fingerprint": fingerprint,
                    "permissions": checked_permissions,
                    "nodeId": self.node_id,
                },
            )
        return self.inspect_pairing_request(request_id)

    def inspect_pairing_request(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            connection = self._connection()
            row = connection.execute(
                "SELECT request_id, device_id, device_uid, name, owner, client_public, "
                "permissions, fingerprint, node_id, requested_at, expires_at, status "
                "FROM pairing_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise _refusal(
                "pairing-request-unknown",
                "no pairing request has that id",
                "pairing-request-unknown",
            )
        return {
            "requestId": row[0],
            "deviceId": row[1],
            "deviceUid": row[2],
            "name": row[3],
            "owner": row[4],
            "clientPublic": row[5],
            "permissions": json.loads(row[6]),
            "fingerprint": row[7],
            "nodeId": row[8],
            "requestedAt": row[9],
            "expiresAt": row[10],
            "status": row[11],
        }

    def approve_pairing_request(
        self,
        request_id: str,
        *,
        operator: str,
        node_id: str,
        device_id: str,
        client_public: str,
        fingerprint: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Approve one pending request after the operator verifies its binding."""

        operator = _text(operator, "operator", limit=200)
        if node_id != self.node_id:
            raise _refusal(
                "pairing-host-mismatch",
                "the approval names a different host node",
                "host-node-mismatch",
                expectedNodeId=self.node_id,
            )
        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            row = connection.execute(
                "SELECT device_id, device_uid, name, owner, client_public, permissions, "
                "fingerprint, node_id, expires_at, status FROM pairing_requests "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise _refusal(
                    "pairing-request-unknown",
                    "no pairing request has that id",
                    "pairing-request-unknown",
                )
            (
                stored_device,
                device_uid,
                name,
                owner,
                stored_public,
                permissions_raw,
                stored_fingerprint,
                stored_node,
                expires_at,
                status,
            ) = row
            if status != "pending":
                raise _refusal(
                    "pairing-request-not-pending",
                    "the pairing request is no longer pending",
                    "pairing-request-not-pending",
                    status=status,
                )
            if expires_at <= current:
                connection.execute(
                    "UPDATE pairing_requests SET status = 'expired' "
                    "WHERE request_id = ?",
                    (request_id,),
                )
                self._bump_sequence(connection)
                raise _refusal(
                    "pairing-request-expired",
                    "the pairing request expired; start pairing again",
                    "pairing-request-expired",
                    commit_state=True,
                )
            if device_id != stored_device or client_public != stored_public:
                raise _refusal(
                    "pairing-device-mismatch",
                    "the approval does not match the pending device binding",
                    "pairing-request-mismatch",
                )
            if not hmac.compare_digest(fingerprint, stored_fingerprint):
                raise _refusal(
                    "pairing-fingerprint-mismatch",
                    "the approval does not match the pending request fingerprint",
                    "pairing-fingerprint-mismatch",
                )
            expected_fingerprint = pairing_fingerprint(
                node_id=stored_node,
                device_id=stored_device,
                device_uid=device_uid,
                name=name,
                owner=owner,
                client_public=stored_public,
                permissions=json.loads(permissions_raw),
            )
            if expected_fingerprint != stored_fingerprint:
                raise _refusal(
                    "pairing-request-corrupt",
                    "the pending request fingerprint does not recompute",
                    "pairing-request-mismatch",
                )
            active = connection.execute(
                "SELECT 1 FROM devices WHERE device_id = ? AND status = 'active'",
                (stored_device,),
            ).fetchone()
            if active is not None:
                raise _refusal(
                    "pairing-device-already-active",
                    "this device is already active; revoke it before re-pairing",
                    "pairing-device-already-active",
                )
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM device_generations "
                    "WHERE device_id = ?",
                    (stored_device,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO devices(device_id, device_uid, name, owner, client_public, "
                "key_generation, status, permissions, approved_at, revoked_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL) "
                "ON CONFLICT(device_id) DO UPDATE SET device_uid = excluded.device_uid, "
                "name = excluded.name, owner = excluded.owner, "
                "client_public = excluded.client_public, "
                "key_generation = excluded.key_generation, status = 'active', "
                "permissions = excluded.permissions, approved_at = excluded.approved_at, "
                "revoked_at = NULL",
                (
                    stored_device,
                    device_uid,
                    name,
                    owner,
                    stored_public,
                    generation,
                    permissions_raw,
                    current,
                ),
            )
            connection.execute(
                "INSERT INTO device_generations(device_id, generation, client_public, "
                "request_id, approved_at) VALUES(?, ?, ?, ?, ?)",
                (stored_device, generation, stored_public, request_id, current),
            )
            connection.execute(
                "UPDATE pairing_requests SET status = 'approved', approved_at = ?, "
                "approved_by = ? WHERE request_id = ?",
                (current, operator, request_id),
            )
            self._audit(
                connection,
                current,
                "pairing-approved",
                device_id=stored_device,
                status="active",
                detail={
                    "requestId": request_id,
                    "fingerprint": stored_fingerprint,
                    "keyGeneration": generation,
                    "operator": operator,
                    "nodeId": self.node_id,
                },
            )
        return self.device_status(stored_device)

    def reject_pairing_request(
        self,
        request_id: str,
        *,
        operator: str,
        node_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        operator = _text(operator, "operator", limit=200)
        if node_id != self.node_id:
            raise _refusal(
                "pairing-host-mismatch",
                "the rejection names a different host node",
                "host-node-mismatch",
            )
        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            updated = connection.execute(
                "UPDATE pairing_requests SET status = 'rejected', rejected_at = ?, "
                "rejected_by = ? WHERE request_id = ? AND status = 'pending'",
                (current, operator, request_id),
            ).rowcount
            if updated != 1:
                raise _refusal(
                    "pairing-request-not-pending",
                    "the pairing request is not pending",
                    "pairing-request-not-pending",
                )
            self._audit(
                connection,
                current,
                "pairing-rejected",
                status="rejected",
                detail={"requestId": request_id, "operator": operator},
            )
        return self.inspect_pairing_request(request_id)

    def _refuse_locked_pairing(
        self, connection: sqlite3.Connection, current: float
    ) -> None:
        raw = self._read_meta(connection, "pair_lock_until")
        if raw is not None and current < float(raw):
            raise _refusal(
                "pairing-rate-limited",
                "too many failed pairing attempts; retry later",
                "pairing-rate-limited",
                retryAfter=float(raw) - current,
            )

    def _record_pairing_failure(
        self, connection: sqlite3.Connection, current: float
    ) -> None:
        raw = self._read_meta(connection, "pair_failures") or "[]"
        try:
            failures = [float(item) for item in json.loads(raw)]
        except (TypeError, ValueError):
            failures = []
        failures = [
            item for item in failures if item > current - GLOBAL_FAILURE_WINDOW_SECONDS
        ]
        failures.append(current)
        if len(failures) >= GLOBAL_FAILURE_LIMIT:
            self._set_meta(connection, "pair_lock_until", repr(
                current + GLOBAL_LOCKOUT_SECONDS
            ))
            failures = []
        self._set_meta(connection, "pair_failures", json.dumps(failures))

    def _clear_pairing_failures(self, connection: sqlite3.Connection) -> None:
        self._set_meta(connection, "pair_failures", "[]")
        connection.execute("DELETE FROM meta WHERE key = 'pair_lock_until'")

    # -- device state -------------------------------------------------------

    def device_status(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection().execute(
                "SELECT device_id, device_uid, name, owner, client_public, "
                "key_generation, status, permissions, approved_at, revoked_at "
                "FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        if row is None:
            raise _refusal(
                "device-unknown",
                "the device is not known to this host",
                "unknown-device",
            )
        return {
            "deviceId": row[0],
            "deviceUid": row[1],
            "name": row[2],
            "owner": row[3],
            "clientPublic": row[4],
            "keyGeneration": row[5],
            "status": row[6],
            "permissions": json.loads(row[7]),
            "approvedAt": row[8],
            "revokedAt": row[9],
        }

    def device_row(self, device_id: str) -> DeviceRow | None:
        try:
            with self._lock:
                row = self._connection().execute(
                    "SELECT owner, client_public, key_generation, status, permissions "
                    "FROM devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
        except MobileAccessError:
            raise
        except sqlite3.Error as error:
            raise _refusal(
                "access-store-unavailable",
                "the access authority cannot answer a device lookup",
                "storage-unavailable",
            ) from error
        if row is None:
            return None
        return DeviceRow(
            owner=str(row[0]),
            client_public=str(row[1]),
            key_generation=int(row[2]),
            status=str(row[3]),
            permissions=frozenset(json.loads(row[4])),
        )

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection().execute(
                "SELECT device_id, device_uid, name, owner, client_public, "
                "key_generation, status, permissions, approved_at, revoked_at "
                "FROM devices ORDER BY approved_at, device_id"
            ).fetchall()
        return [
            {
                "deviceId": row[0],
                "deviceUid": row[1],
                "name": row[2],
                "owner": row[3],
                "clientPublic": row[4],
                "keyGeneration": row[5],
                "status": row[6],
                "permissions": json.loads(row[7]),
                "approvedAt": row[8],
                "revokedAt": row[9],
            }
            for row in rows
        ]

    def revoke_device(
        self,
        device_id: str,
        *,
        operator: str,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Commit revocation at ``t0``; subsequent admissions are refused."""

        operator = _text(operator, "operator", limit=200)
        reason = _text(reason, "reason", limit=200)
        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            row = connection.execute(
                "SELECT key_generation, status, revoked_at FROM devices "
                "WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                raise _refusal(
                    "device-unknown",
                    "the device is not known to this host",
                    "unknown-device",
                )
            generation, status, revoked_at = int(row[0]), str(row[1]), row[2]
            if status == "revoked":
                return {
                    "deviceId": device_id,
                    "t0": float(revoked_at),
                    "keyGeneration": generation,
                    "alreadyRevoked": True,
                }
            connection.execute(
                "UPDATE devices SET status = 'revoked', revoked_at = ? "
                "WHERE device_id = ?",
                (current, device_id),
            )
            connection.execute(
                "UPDATE device_generations SET revoked_at = ? "
                "WHERE device_id = ? AND generation = ?",
                (current, device_id, generation),
            )
            connection.execute(
                "UPDATE session_grants SET revoked_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (current, device_id),
            )
            self._audit(
                connection,
                current,
                "device-revoked",
                device_id=device_id,
                status="revoked",
                detail={
                    "keyGeneration": generation,
                    "operator": operator,
                    "reason": reason,
                    "t0": current,
                },
            )
        return {
            "deviceId": device_id,
            "t0": current,
            "keyGeneration": generation,
            "alreadyRevoked": False,
        }

    # -- explicit session grants -------------------------------------------

    def grant_session(
        self,
        device_id: str,
        session_id: str,
        *,
        session_owner: str,
        operator: str,
        source_device_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Grant one selected session; never owner-wide and never implicit."""

        session_id = _identifier(session_id, "sessionId")
        session_owner = _text(session_owner, "sessionOwner")
        operator = _text(operator, "operator", limit=200)
        if source_device_id is not None:
            source_device_id = _identifier(source_device_id, "sourceDeviceId")
            if source_device_id == device_id:
                raise _refusal(
                    "session-share-invalid",
                    "a session share needs a different source and target device",
                    "session-share-invalid",
                )
        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            device = connection.execute(
                "SELECT owner, status FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if device is None or device[1] != "active":
                raise _refusal(
                    "device-revoked" if device is not None else "device-unknown",
                    "only an active device may receive a session grant",
                    "device-revoked" if device is not None else "unknown-device",
                )
            if str(device[0]) != session_owner:
                raise _refusal(
                    "owner-mismatch",
                    "a device may only receive sessions owned by its own owner",
                    "owner-mismatch",
                )
            shared_from: str | None = None
            if source_device_id is not None:
                source = connection.execute(
                    "SELECT owner, status FROM devices WHERE device_id = ?",
                    (source_device_id,),
                ).fetchone()
                if source is None or source[1] != "active" or str(source[0]) != session_owner:
                    raise _refusal(
                        "session-share-invalid",
                        "the sharing source is not an active device of the same owner",
                        "session-share-invalid",
                    )
                grant = connection.execute(
                    "SELECT 1 FROM session_grants WHERE device_id = ? AND session_id = ? "
                    "AND owner = ? AND revoked_at IS NULL",
                    (source_device_id, session_id, session_owner),
                ).fetchone()
                if grant is None:
                    raise _refusal(
                        "session-not-granted",
                        "the sharing source holds no grant for this session",
                        "session-not-granted",
                    )
                shared_from = source_device_id
            connection.execute(
                "INSERT INTO session_grants(device_id, session_id, owner, granted_at, "
                "granted_by, shared_from, revoked_at) VALUES(?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(device_id, session_id) DO UPDATE SET owner = excluded.owner, "
                "granted_at = excluded.granted_at, granted_by = excluded.granted_by, "
                "shared_from = excluded.shared_from, revoked_at = NULL",
                (
                    device_id,
                    session_id,
                    session_owner,
                    current,
                    operator,
                    shared_from,
                ),
            )
            sharing = "shared" if shared_from else "isolated"
            self._audit(
                connection,
                current,
                "session-granted",
                device_id=device_id,
                session_id=session_id,
                sharing=sharing,
                source_device_id=shared_from,
                status="active",
                detail={"operator": operator, "owner": session_owner},
            )
        return self.grant_status(device_id, session_id)

    def revoke_session_grant(
        self,
        device_id: str,
        session_id: str,
        *,
        operator: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        operator = _text(operator, "operator", limit=200)
        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            updated = connection.execute(
                "UPDATE session_grants SET revoked_at = ? "
                "WHERE device_id = ? AND session_id = ? AND revoked_at IS NULL",
                (current, device_id, session_id),
            ).rowcount
            if updated != 1:
                raise _refusal(
                    "session-grant-unknown",
                    "no active grant exists for that device and session",
                    "session-grant-unknown",
                )
            self._audit(
                connection,
                current,
                "session-revoked",
                device_id=device_id,
                session_id=session_id,
                status="revoked",
                detail={"operator": operator},
            )
        return {"deviceId": device_id, "sessionId": session_id, "revokedAt": current}

    def granted_sessions(self, device_id: str) -> set[str]:
        """The session ids actively granted to ``device_id``.

        This is the list-visibility projection: an ungranted device must not
        learn a session id from ``sessions.list`` just because the composition
        root configured it.  A cancelled grant disappears immediately; a
        storage fault is a definitive refusal, never an empty-but-successful
        list that hides the fault from the caller.
        """

        try:
            with self._lock:
                rows = self._connection().execute(
                    "SELECT session_id FROM session_grants "
                    "WHERE device_id = ? AND revoked_at IS NULL",
                    (device_id,),
                ).fetchall()
        except MobileAccessError:
            raise
        except sqlite3.Error as error:
            raise _refusal(
                "access-store-unavailable",
                "the access authority cannot answer a session-grant lookup",
                "storage-unavailable",
            ) from error
        return {str(row[0]) for row in rows}

    def grant_status(self, device_id: str, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection().execute(
                "SELECT owner, granted_at, granted_by, shared_from, revoked_at "
                "FROM session_grants WHERE device_id = ? AND session_id = ?",
                (device_id, session_id),
            ).fetchone()
        if row is None:
            raise _refusal(
                "session-grant-unknown",
                "no grant exists for that device and session",
                "session-grant-unknown",
            )
        return {
            "deviceId": device_id,
            "sessionId": session_id,
            "owner": row[0],
            "grantedAt": row[1],
            "grantedBy": row[2],
            "sharedFrom": row[3],
            "revokedAt": row[4],
            "sharing": "shared" if row[3] else "isolated",
        }

    def list_grants(self, device_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if device_id is None:
                rows = self._connection().execute(
                    "SELECT device_id, session_id, owner, granted_at, granted_by, "
                    "shared_from, revoked_at FROM session_grants "
                    "ORDER BY granted_at, device_id, session_id"
                ).fetchall()
            else:
                rows = self._connection().execute(
                    "SELECT device_id, session_id, owner, granted_at, granted_by, "
                    "shared_from, revoked_at FROM session_grants WHERE device_id = ? "
                    "ORDER BY granted_at, session_id",
                    (device_id,),
                ).fetchall()
        return [
            {
                "deviceId": row[0],
                "sessionId": row[1],
                "owner": row[2],
                "grantedAt": row[3],
                "grantedBy": row[4],
                "sharedFrom": row[5],
                "revokedAt": row[6],
                "sharing": "shared" if row[5] else "isolated",
            }
            for row in rows
        ]

    # -- replay and admission ----------------------------------------------
