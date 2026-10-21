"""Frozen vocabulary for the durable mobile-access authority.

Pairing codes and their limits, the operator-facing error shape, the protocol
identifier validation and the approval fingerprint live here so the store,
the replay/admission mixin and the operator CLI all speak one vocabulary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets

#: Pairing codes are 10 characters from a 31-symbol alphabet (~49.5 bits).
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
PAIRING_CODE_TTL_SECONDS = 300.0
PAIRING_REQUEST_TTL_SECONDS = 300.0
CODE_MAX_ATTEMPTS = 5
GLOBAL_FAILURE_WINDOW_SECONDS = 300.0
GLOBAL_FAILURE_LIMIT = 10
GLOBAL_LOCKOUT_SECONDS = 60.0
CLOCK_REGRESSION_SECONDS = 5.0
NONCE_CAPACITY_PER_DEVICE = 4096
PAIRING_ATTEMPT_BYTES = 32
PAIRING_ATTEMPT_LENGTH = 43

SUPPORTED_PERMISSIONS = frozenset(
    {"sessions.read", "sessions.write", "sessions.events"}
)
OPERATION_PERMISSIONS = {
    "sessions.history": "sessions.read",
    "sessions.events": "sessions.events",
    "sessions.send": "sessions.write",
}
SESSIONLESS_OPERATIONS = frozenset({"capabilities.get", "sessions.list"})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,255}$")
_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,511}$")
_PAIRING_ATTEMPT = re.compile(r"^[A-Za-z0-9_-]{43}$")


class MobileAccessError(Exception):
    """One named refusal from the durable authority.

    ``code`` is the facade's error-frame code; ``reason`` is the frozen
    reference reason carried in ``details.reason`` and in telemetry.  The
    values never include a pairing code, a request key or key material.
    """

    def __init__(
        self,
        code: str,
        message: str,
        reason: str | None = None,
        *,
        commit_state: bool = False,
        **details: object,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason or code
        self.commit_state = commit_state
        self.details = details


def _refusal(
    code: str,
    message: str,
    reason: str | None = None,
    *,
    commit_state: bool = False,
    **details: object,
) -> MobileAccessError:
    return MobileAccessError(
        code, message, reason, commit_state=commit_state, **details
    )


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _refusal(
            "pairing-request-invalid",
            f"{field} must be a non-empty protocol identifier",
            "malformed-pairing-request",
            field=field,
        )
    return value


def _public_key(value: object) -> str:
    if not isinstance(value, str) or not _PUBLIC_KEY.fullmatch(value):
        raise _refusal(
            "pairing-request-invalid",
            "clientPublic must be a non-empty public-key identifier",
            "malformed-pairing-request",
            field="clientPublic",
        )
    return value


def _text(value: object, field: str, *, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 0x20 for character in value)
    ):
        raise _refusal(
            "pairing-request-invalid",
            f"{field} must be a non-empty bounded string",
            "malformed-pairing-request",
            field=field,
        )
    return value


def _permissions(value: object) -> list[str]:
    if value is None:
        return ["sessions.read", "sessions.write", "sessions.events"]
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise _refusal(
            "pairing-request-invalid",
            "permissions must be a list of permission names",
            "malformed-pairing-request",
            field="permissions",
        )
    unknown = sorted(set(value) - SUPPORTED_PERMISSIONS)
    if unknown:
        raise _refusal(
            "pairing-request-invalid",
            "permissions names an unknown capability",
            "malformed-pairing-request",
            unknown=unknown,
        )
    return sorted(set(value))


def normalize_pairing_code(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("-", "").replace(" ", "").strip().upper()


def new_pairing_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _pairing_attempt(value: object) -> str:
    """Validate one canonical 32-byte base64url pairing attempt id.

    The id itself is never persisted; the store persists only
    :func:`pairing_attempt_hash` so a database copy cannot resume a pairing.
    """

    if not isinstance(value, str) or not _PAIRING_ATTEMPT.fullmatch(value):
        raise _refusal(
            "pairing-request-invalid",
            "pairingAttemptId must be a canonical 32-byte base64url identifier",
            "malformed-pairing-request",
            field="pairingAttemptId",
        )
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + padding)
    except (binascii.Error, ValueError) as error:
        raise _refusal(
            "pairing-request-invalid",
            "pairingAttemptId must be a canonical 32-byte base64url identifier",
            "malformed-pairing-request",
            field="pairingAttemptId",
        ) from error
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != PAIRING_ATTEMPT_BYTES or canonical != value:
        raise _refusal(
            "pairing-request-invalid",
            "pairingAttemptId must be a canonical 32-byte base64url identifier",
            "malformed-pairing-request",
            field="pairingAttemptId",
        )
    return value


def pairing_attempt_hash(attempt_id: str) -> str:
    return hashlib.sha256(attempt_id.encode("ascii")).hexdigest()


def pairing_fingerprint(
    *,
    node_id: str,
    device_id: str,
    device_uid: str,
    name: str,
    owner: str,
    client_public: str,
    permissions: list[str],
) -> str:
    """The immutable binding an operator inspects and approves."""

    canonical = json.dumps(
        {
            "clientPublic": client_public,
            "deviceId": device_id,
            "deviceUid": device_uid,
            "name": name,
            "nodeId": node_id,
            "owner": owner,
            "permissions": sorted(set(permissions)),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
