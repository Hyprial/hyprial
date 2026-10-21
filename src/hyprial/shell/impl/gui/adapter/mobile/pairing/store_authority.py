"""The pairing-attempt authority bound to the durable SQLite store.

S1 defines the pure state machine and its narrow ``PairingAuthority``
protocol.  This mixin implements that protocol on ``MobileAccessStore``: one
transaction binds the attempt hash, consumes the code and writes the request;
retries and resumes read by attempt hash before any code-guess counter is
touched.  Status reads are projections only and never persist a transition.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
from typing import Any

from hyprial.shell.impl.gui.adapter.mobile.models import (
    CLOCK_REGRESSION_SECONDS,
    CODE_MAX_ATTEMPTS,
    MobileAccessError,
    _identifier,
    _pairing_attempt,
    _permissions,
    _public_key,
    _refusal,
    _text,
    normalize_pairing_code,
    pairing_attempt_hash,
    pairing_fingerprint,
)
from hyprial.shell.impl.gui.adapter.mobile.pairing.authority import AuthorityError

_REQUEST_COLUMNS = (
    "request_id, device_id, device_uid, name, owner, client_public, "
    "permissions, fingerprint, node_id, requested_at, expires_at, status, "
    "approved_at, rejected_at, code_hash, pairing_attempt_hash"
)


def _authority_error(error: MobileAccessError) -> AuthorityError:
    """Convert the store's internal refusal at the S1 authority boundary."""

    details = dict(error.details)
    field = details.pop("field", None)
    retry = details.pop("retryAfter", None)
    retry_after: int | None = None
    if retry is not None:
        try:
            retry_after = int(math.ceil(float(retry)))
        except (TypeError, ValueError, OverflowError):
            retry_after = 60
    return AuthorityError(
        error.code,
        error.message,
        field=field if isinstance(field, str) else None,
        retry_after_seconds=retry_after,
        **details,
    )


class PairingAttemptMixin:
    """Attempt-aware pairing methods mixed into ``MobileAccessStore``."""

    # -- attempt-aware redeem ---------------------------------------------

    def _redeem_pairing_attempt(
        self,
        *,
        code: object,
        device_id: object,
        device_uid: object,
        name: object,
        owner: object,
        client_public: object,
        permissions: object,
        pairing_attempt_id: object,
        mode: object,
        now: float | None,
        request_ttl_seconds: float,
    ) -> dict[str, Any]:
        try:
            return self._redeem_pairing_attempt_locked(
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
        except MobileAccessError as error:
            raise _authority_error(error) from None
        except sqlite3.IntegrityError as error:
            # The partial unique index is the cross-process arbiter: a racing
            # attempt that slipped past the attempt lookup must not become a
            # second request.
            raise AuthorityError(
                "pairing-attempt-conflict",
                "pairingAttemptId was already bound to a request",
            ) from error
        except sqlite3.Error as error:
            raise AuthorityError(
                "pairing-unavailable", "the pairing authority is unavailable"
            ) from error

    def _redeem_pairing_attempt_locked(
        self,
        *,
        code: object,
        device_id: object,
        device_uid: object,
        name: object,
        owner: object,
        client_public: object,
        permissions: object,
        pairing_attempt_id: object,
        mode: object,
        now: float | None,
        request_ttl_seconds: float,
    ) -> dict[str, Any]:
        attempt = _pairing_attempt(pairing_attempt_id)
        attempt_hash = pairing_attempt_hash(attempt)
        if not isinstance(mode, str) or mode not in {"start", "resume"}:
            raise _refusal(
                "pairing-request-invalid",
                "mode must be start or resume",
                "malformed-pairing-request",
                field="mode",
            )
        checked_device = _identifier(device_id, "deviceId")
        checked_uid = _identifier(device_uid, "deviceUid")
        checked_name = _text(name, "name", limit=200)
        checked_owner = _text(owner, "owner")
        checked_public = _public_key(client_public)
        checked_permissions = _permissions(permissions)
        normalized = normalize_pairing_code(code)

        with self._transaction() as connection:
            current = self._checked_now(connection, now)
            row = connection.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM pairing_requests "
                "WHERE pairing_attempt_hash = ?",
                (attempt_hash,),
            ).fetchone()
            if row is not None:
                if not self._attempt_fields_match(
                    connection,
                    row,
                    mode=mode,
                    device_id=checked_device,
                    device_uid=checked_uid,
                    name=checked_name,
                    owner=checked_owner,
                    client_public=checked_public,
                    permissions=checked_permissions,
                    normalized_code=normalized,
                ):
                    raise _refusal(
                        "pairing-attempt-conflict",
                        "pairingAttemptId was already bound to other fields",
                        "pairing-attempt-conflict",
                    )
                return self._request_projection(connection, row, current)

            if mode == "resume":
                raise _refusal(
                    "pairing-request-unknown",
                    "no pairing request exists for this pairing attempt",
                    "pairing-request-unknown",
                )

            self._refuse_locked_pairing(connection, current)
            code_row = connection.execute(
                "SELECT code_hash, salt, expires_at, consumed_at, attempts, active "
                "FROM pairing_codes ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if code_row is None:
                self._record_pairing_failure(connection, current)
                raise _refusal(
                    "pairing-code-invalid",
                    "the pairing code is missing or does not match",
                    "pairing-code-invalid",
                    commit_state=True,
                )
            (
                stored_code_hash,
                salt,
                code_expires_at,
                consumed_at,
                attempts,
                active,
            ) = code_row
            if consumed_at is not None:
                self._record_pairing_failure(connection, current)
                raise _refusal(
                    "pairing-code-consumed",
                    "this pairing code has already been redeemed",
                    "pairing-code-consumed",
                    commit_state=True,
                )
            if not active or float(code_expires_at) <= current:
                connection.execute(
                    "UPDATE pairing_codes SET active = 0 WHERE code_hash = ?",
                    (stored_code_hash,),
                )
                self._record_pairing_failure(connection, current)
                raise _refusal(
                    "pairing-code-expired",
                    "this pairing code has expired; start pairing again",
                    "pairing-code-expired",
                    commit_state=True,
                )
            expected_hash = hashlib.sha256(
                (str(salt) + normalized).encode("ascii")
            ).hexdigest()
            if not hmac.compare_digest(expected_hash, str(stored_code_hash)):
                updated = int(attempts) + 1
                connection.execute(
                    "UPDATE pairing_codes SET attempts = ?, active = ? "
                    "WHERE code_hash = ?",
                    (
                        updated,
                        0 if updated >= CODE_MAX_ATTEMPTS else 1,
                        stored_code_hash,
                    ),
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
                (current, stored_code_hash),
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
                current + float(request_ttl_seconds),
                float(code_expires_at),
            )
            connection.execute(
                "INSERT INTO pairing_requests(request_id, device_id, device_uid, "
                "name, owner, client_public, permissions, fingerprint, node_id, "
                "requested_at, expires_at, status, pairing_attempt_hash, "
                "code_hash) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', "
                "?, ?)",
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
                    attempt_hash,
                    str(stored_code_hash),
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
            inserted = connection.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM pairing_requests "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if inserted is None:
                raise _refusal(
                    "pairing-request-corrupt",
                    "the pairing request could not be read back",
                    "pairing-request-corrupt",
                )
            return self._request_projection(connection, inserted, current)

    def _attempt_fields_match(
        self,
        connection: sqlite3.Connection,
        row: tuple,
        *,
        mode: str,
        device_id: str,
        device_uid: str,
        name: str,
        owner: str,
        client_public: str,
        permissions: list[str],
        normalized_code: str,
    ) -> bool:
        stored = (
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            json.loads(row[6]),
        )
        expected = (
            device_id,
            device_uid,
            name,
            owner,
            client_public,
            permissions,
        )
        if stored != expected:
            return False
        if mode == "resume":
            return True
        stored_code_hash = row[14]
        if stored_code_hash is None:
            return False
        salt_row = connection.execute(
            "SELECT salt FROM pairing_codes WHERE code_hash = ?",
            (stored_code_hash,),
        ).fetchone()
        if salt_row is None:
            return False
        expected_hash = hashlib.sha256(
            (str(salt_row[0]) + normalized_code).encode("ascii")
        ).hexdigest()
        return hmac.compare_digest(expected_hash, str(stored_code_hash))

    # -- status projection -------------------------------------------------

    def pairing_status(
        self,
        request_id: str,
        *,
        pairing_attempt_id: object,
        now: float | None = None,
    ) -> dict[str, Any]:
        try:
            attempt_hash = pairing_attempt_hash(
                _pairing_attempt(pairing_attempt_id)
            )
            if not isinstance(request_id, str) or not request_id:
                raise _refusal(
                    "pairing-request-unknown",
                    "no pairing request has that id",
                    "pairing-request-unknown",
                )
            with self._lock:
                connection = self._connection()
                current = self._read_only_now(connection, now)
                row = connection.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM pairing_requests "
                    "WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if (
                    row is None
                    or row[15] is None
                    or not hmac.compare_digest(str(row[15]), attempt_hash)
                ):
                    raise _refusal(
                        "pairing-request-unknown",
                        "no pairing request has that id",
                        "pairing-request-unknown",
                    )
                return self._request_projection(connection, row, current)
        except MobileAccessError as error:
            raise _authority_error(error) from None
        except sqlite3.Error as error:
            raise AuthorityError(
                "pairing-unavailable", "the pairing authority is unavailable"
            ) from error

    def _read_only_now(
        self, connection: sqlite3.Connection, now: float | None
    ) -> float:
        """Check clock regression without writing ``last_now``."""

        value = float(self.clock() if now is None else now)
        last_raw = self._read_meta(connection, "last_now")
        last = float(last_raw) if last_raw is not None else 0.0
        if last and value < last - CLOCK_REGRESSION_SECONDS:
            raise _refusal(
                "auth-clock-anomaly",
                "the authorization clock moved backwards; refusing to widen a window",
                "clock-anomaly",
                now=value,
                lastObserved=last,
            )
        return value

    def _request_projection(
        self,
        connection: sqlite3.Connection,
        row: tuple,
        current: float,
    ) -> dict[str, Any]:
        (
            request_id,
            device_id,
            _device_uid,
            _name,
            owner,
            client_public,
            permissions_raw,
            fingerprint,
            node_id,
            requested_at,
            expires_at,
            status,
            _approved_at,
            rejected_at,
            _code_hash,
            _attempt_hash,
        ) = row
        requested = int(requested_at)
        expires = int(expires_at)
        projection: dict[str, Any] = {
            "requestId": str(request_id),
            "nodeId": str(node_id),
            "deviceId": str(device_id),
            "clientPublic": str(client_public),
            "owner": str(owner),
            "permissions": json.loads(permissions_raw),
            "fingerprint": str(fingerprint),
            "requestedAt": requested,
            "expiresAt": expires,
            "status": str(status),
        }
        if status == "pending":
            if current >= float(expires_at):
                projection["status"] = "expired"
            else:
                projection["pollAfterMs"] = 2000
        elif status == "rejected":
            if rejected_at is None:
                raise _refusal(
                    "pairing-request-corrupt",
                    "the rejected pairing request has no rejection time",
                    "pairing-request-corrupt",
                )
            projection["rejectedAt"] = int(rejected_at)
        elif status == "approved":
            device = self._approved_device(connection, str(request_id))
            projection["device"] = device
            if device["status"] == "revoked":
                projection["status"] = "revoked"
        return projection

    def _approved_device(
        self, connection: sqlite3.Connection, request_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT g.generation, g.client_public, g.approved_at, g.revoked_at, "
            "d.device_id, d.owner, d.permissions, d.status, d.key_generation, "
            "d.revoked_at FROM device_generations g "
            "JOIN devices d ON d.device_id = g.device_id "
            "WHERE g.request_id = ? ORDER BY g.generation DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        if row is None:
            raise _refusal(
                "pairing-request-corrupt",
                "the approved pairing request has no device generation",
                "pairing-request-corrupt",
            )
        (
            generation,
            client_public,
            approved_at,
            generation_revoked_at,
            device_id,
            owner,
            permissions_raw,
            device_status,
            device_generation,
            device_revoked_at,
        ) = row
        revoked_at = generation_revoked_at
        if (
            revoked_at is None
            and device_status == "revoked"
            and int(device_generation) == int(generation)
        ):
            revoked_at = device_revoked_at
        revoked = revoked_at is not None
        return {
            "deviceId": str(device_id),
            "deviceGeneration": int(generation),
            "targetNodeId": self.node_id,
            "clientPublic": str(client_public),
            "owner": str(owner),
            "permissions": json.loads(permissions_raw),
            "approvedAt": int(approved_at),
            "status": "revoked" if revoked else "active",
            "revokedAt": int(revoked_at) if revoked_at is not None else None,
        }
