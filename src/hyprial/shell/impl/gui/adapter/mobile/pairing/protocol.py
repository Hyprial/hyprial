"""Closed-set framing and vocabulary for the S1 pairing protocol.

One frame is exactly one UTF-8 JSON object followed by one newline.  The
parser rejects duplicate keys, unknown fields, missing fields, non-finite
numbers, bool-as-int, trailing bytes and frames over 8192 bytes.  It exposes
no approve, reject, device or grant operation: those local/operator and
business actions live outside this wire protocol.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

from ..models import SUPPORTED_PERMISSIONS, normalize_pairing_code
from .crypto import (
    CryptoError,
    b64url_decode,
    b64url_encode,
    decode_public_key,
)

PROTOCOL = "m11-pairing/1"
DERIVE_PROFILE = "p256-hkdf-sha256-v1"
FRAME_VERSION = 1
MAX_FRAME_BYTES = 8192

NONCE_BYTES = 32
PUBLIC_KEY_BYTES = 65
SIGNATURE_BYTES = 64
PROOF_BYTES = 32
ATTEMPT_BYTES = 32
MAX_NAME_LENGTH = 200
MAX_OWNER_LENGTH = 512
MAX_CODE_LENGTH = 64
POLL_AFTER_MIN_MS = 1_000
POLL_AFTER_MAX_MS = 5_000

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"
REVOKED = "revoked"
_STATUSES = frozenset({PENDING, APPROVED, REJECTED, EXPIRED, REVOKED})

PUBLIC_ERROR_CODES = frozenset(
    {
        "pairing-frame-invalid",
        "pairing-protocol-unsupported",
        "pairing-host-mismatch",
        "pairing-channel-unbound",
        "pairing-client-proof-invalid",
        "pairing-request-invalid",
        "pairing-code-invalid",
        "pairing-rate-limited",
        "pairing-attempt-conflict",
        "pairing-request-unknown",
        "pairing-unavailable",
        "pairing-internal",
    }
)

INTERNAL_ERROR_CODES = frozenset(
    {
        "pairing-code-consumed",
        "pairing-code-expired",
        "pairing-request-not-pending",
        "pairing-request-expired",
        "pairing-device-mismatch",
        "pairing-fingerprint-mismatch",
        "pairing-request-corrupt",
        "pairing-device-already-active",
        "access-store-unavailable",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+=-]{0,255}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CHANNEL_BINDING = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^pair-[0-9a-f]{32}$")
_PUBLIC_KEY_PREFIX = b"\x04"


class PairingError(Exception):
    """The only public refusal shape of the pairing protocol.

    Internal store reasons are folded to a public code by
    :func:`authority_refusal`; an internal code cannot be constructed as a
    wire error and is replaced by ``pairing-internal`` with a generic message.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        retry_after_seconds: int | None = None,
        close: bool = True,
    ) -> None:
        if code not in PUBLIC_ERROR_CODES:
            code = "pairing-internal"
            message = "pairing failed"
            field = None
            retry_after_seconds = None
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = (
            field
            if isinstance(field, str) and 1 <= len(field) <= 64
            else None
        )
        if self.code == "pairing-rate-limited":
            if (
                type(retry_after_seconds) is not int
                or not 1 <= retry_after_seconds <= 3_600
            ):
                retry_after_seconds = 60
            self.retry_after_seconds = retry_after_seconds
        else:
            self.retry_after_seconds = None
        self.close = bool(close)

    def as_frame(self) -> dict:
        frame = {
            "v": FRAME_VERSION,
            "type": "pairing.error",
            "code": self.code,
            "message": self.message,
        }
        if self.field is not None:
            frame["field"] = self.field
        if self.code == "pairing-rate-limited":
            frame["retryAfterSeconds"] = self.retry_after_seconds
        return frame


def _frame_invalid(field: str | None = None) -> PairingError:
    return PairingError(
        "pairing-frame-invalid", "pairing frame is invalid", field=field
    )


def _request_invalid(field: str, message: str | None = None) -> PairingError:
    return PairingError(
        "pairing-request-invalid",
        message or "pairing request field is invalid",
        field=field,
    )


def _protocol_unsupported() -> PairingError:
    return PairingError(
        "pairing-protocol-unsupported",
        "pairing protocol version is unsupported",
    )


def _code_invalid() -> PairingError:
    return PairingError(
        "pairing-code-invalid", "pairing code is invalid"
    )


def authority_refusal(
    code: str,
    message: str | None = None,
    *,
    field: str | None = None,
    details: Mapping | None = None,
) -> PairingError:
    """Fold an internal authority reason into a public error.

    The supplied ``message`` is intentionally ignored: internal reasons can
    mention consumption, expiry, fingerprints or request state and must not
    reach the peer.  ``details.retryAfter`` is the only value carried through.
    """

    details = dict(details or {})
    field = field or (
        details.get("field") if isinstance(details.get("field"), str) else None
    )
    if code in {"pairing-code-invalid", "pairing-code-consumed", "pairing-code-expired"}:
        return _code_invalid()
    if code == "pairing-rate-limited":
        retry = details.get("retryAfter", details.get("retry_after_seconds"))
        try:
            seconds = int(math.ceil(float(retry))) if retry is not None else 60
        except (TypeError, ValueError, OverflowError):
            seconds = 60
        return PairingError(
            "pairing-rate-limited",
            "pairing is temporarily rate limited",
            retry_after_seconds=seconds,
            close=True,
        )
    if code == "pairing-attempt-conflict":
        return PairingError(
            "pairing-attempt-conflict",
            "pairing attempt is already bound to another request",
            close=True,
        )
    if code in {"pairing-request-unknown", "pairing-request-expired"}:
        return PairingError(
            "pairing-request-unknown", "pairing request is unknown"
        )
    if code in {
        "pairing-unavailable",
        "access-store-unavailable",
        "pairing-request-corrupt",
        "pairing-request-not-pending",
    }:
        return PairingError(
            "pairing-unavailable", "pairing is unavailable"
        )
    if code == "pairing-host-mismatch":
        return PairingError(
            "pairing-host-mismatch", "pairing host does not match"
        )
    if code == "pairing-channel-unbound":
        return PairingError(
            "pairing-channel-unbound", "pairing channel is not bound"
        )
    if code == "pairing-client-proof-invalid":
        return PairingError(
            "pairing-client-proof-invalid", "pairing proof is invalid"
        )
    if code == "pairing-request-invalid":
        return _request_invalid(field or "request")
    return PairingError("pairing-internal", "pairing failed")


def _require_protocol(value: object) -> None:
    if not isinstance(value, str) or value != PROTOCOL:
        raise _protocol_unsupported()


def _int_field(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise _frame_invalid(field)
    if value < minimum or (maximum is not None and value > maximum):
        raise _frame_invalid(field)
    return value


def _identifier_field(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _request_invalid(field, f"{field} is not a protocol identifier")
    return value


def _text_field(value: object, field: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 0x20 for character in value)
    ):
        raise _request_invalid(field, f"{field} is not a bounded text value")
    return value


def _wire_b64(value: object, field: str, size: int) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, str):
        try:
            raw = b64url_decode(value)
        except CryptoError as error:
            raise _frame_invalid(field) from error
    else:
        raise _frame_invalid(field)
    if len(raw) != size:
        raise _frame_invalid(field)
    return b64url_encode(raw)


def _wire_public(value: object, field: str) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, str):
        try:
            raw = b64url_decode(value)
        except CryptoError as error:
            raise _request_invalid(field) from error
    else:
        raise _request_invalid(field)
    if len(raw) != PUBLIC_KEY_BYTES or raw[:1] != _PUBLIC_KEY_PREFIX:
        raise _request_invalid(field)
    try:
        decode_public_key(b64url_encode(raw))
    except CryptoError as error:
        raise _request_invalid(field) from error
    return b64url_encode(raw)


def _fingerprint_field(value: object, field: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise _frame_invalid(field)
    return value


def _status_fingerprint_field(value: object) -> str:
    if not isinstance(value, str) or not _HEX_FINGERPRINT.fullmatch(value):
        raise _frame_invalid("fingerprint")
    return value


def _channel_field(value: object) -> str:
    if not isinstance(value, str) or not _CHANNEL_BINDING.fullmatch(value):
        raise _frame_invalid("channelBinding")
    return value


def _request_id_field(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise _frame_invalid("requestId")
    return value


def _permissions_field(value: object) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise _request_invalid(
            "permissions", "permissions must be a non-empty list of names"
        )
    unknown = sorted(set(value) - SUPPORTED_PERMISSIONS)
    if unknown:
        raise _request_invalid(
            "permissions", "permissions names an unknown capability"
        )
    return sorted(set(value))


def _code_field(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CODE_LENGTH:
        raise _code_invalid()
    return normalize_pairing_code(value)


def _mode_field(value: object) -> str:
    if value not in {"start", "resume"}:
        raise _frame_invalid("mode")
    return value


def _status_field(value: object) -> str:
    if value not in _STATUSES:
        raise _frame_invalid("status")
    return value


def _check_keys(
    frame: Mapping, required: set[str], optional: set[str] | None = None
) -> None:
    optional = optional or set()
    keys = set(frame)
    if required - keys:
        raise _frame_invalid(next(iter(sorted(required - keys))))
    if keys - required - optional:
        raise _frame_invalid(next(iter(sorted(keys - required - optional))))


def _hello_frame(frame: Mapping) -> dict:
    _check_keys(
        frame,
        {
            "v",
            "type",
            "protocol",
            "nodeId",
            "hostKeyFingerprint",
            "clientNonce",
            "clientPublic",
        },
    )
    _require_protocol(frame["protocol"])
    return {
        "v": FRAME_VERSION,
        "type": "pairing.hello",
        "protocol": PROTOCOL,
        "nodeId": _identifier_field(frame["nodeId"], "nodeId"),
        "hostKeyFingerprint": _fingerprint_field(
            frame["hostKeyFingerprint"], "hostKeyFingerprint"
        ),
        "clientNonce": _wire_b64(frame["clientNonce"], "clientNonce", NONCE_BYTES),
        "clientPublic": _wire_public(frame["clientPublic"], "clientPublic"),
    }


def _challenge_frame(frame: Mapping) -> dict:
    _check_keys(
        frame,
        {
            "v",
            "type",
            "protocol",
            "nodeId",
            "hostKeyFingerprint",
            "clientNonce",
            "clientPublic",
            "hostNonce",
            "hostDerivePublic",
            "deriveProfile",
            "serverTime",
            "signature",
        },
    )
    _require_protocol(frame["protocol"])
    if frame["deriveProfile"] != DERIVE_PROFILE:
        raise _protocol_unsupported()
    return {
        "v": FRAME_VERSION,
        "type": "pairing.challenge",
        "protocol": PROTOCOL,
        "nodeId": _identifier_field(frame["nodeId"], "nodeId"),
        "hostKeyFingerprint": _fingerprint_field(
            frame["hostKeyFingerprint"], "hostKeyFingerprint"
        ),
        "clientNonce": _wire_b64(frame["clientNonce"], "clientNonce", NONCE_BYTES),
        "clientPublic": _wire_public(frame["clientPublic"], "clientPublic"),
        "hostNonce": _wire_b64(frame["hostNonce"], "hostNonce", NONCE_BYTES),
        "hostDerivePublic": _wire_public(
            frame["hostDerivePublic"], "hostDerivePublic"
        ),
        "deriveProfile": DERIVE_PROFILE,
        "serverTime": _int_field(frame["serverTime"], "serverTime"),
        "signature": _wire_b64(
            frame["signature"], "signature", SIGNATURE_BYTES
        ),
    }


def _redeem_frame(frame: Mapping) -> dict:
    required = {
        "v",
        "type",
        "mode",
        "channelBinding",
        "pairingAttemptId",
        "deviceId",
        "deviceUid",
        "name",
        "owner",
        "clientPublic",
        "permissions",
        "clientProof",
    }
    _check_keys(frame, required, {"code"})
    mode = _mode_field(frame["mode"])
    if mode == "start" and "code" not in frame:
        raise _frame_invalid("code")
    if mode == "resume" and "code" in frame:
        raise _frame_invalid("code")
    normalised = {
        "v": FRAME_VERSION,
        "type": "pairing.redeem",
        "mode": mode,
        "channelBinding": _channel_field(frame["channelBinding"]),
        "pairingAttemptId": _wire_b64(
            frame["pairingAttemptId"], "pairingAttemptId", ATTEMPT_BYTES
        ),
        "deviceId": _identifier_field(frame["deviceId"], "deviceId"),
        "deviceUid": _identifier_field(frame["deviceUid"], "deviceUid"),
        "name": _text_field(frame["name"], "name", MAX_NAME_LENGTH),
        "owner": _text_field(frame["owner"], "owner", MAX_OWNER_LENGTH),
        "clientPublic": _wire_public(frame["clientPublic"], "clientPublic"),
        "permissions": _permissions_field(frame["permissions"]),
        "clientProof": _wire_b64(
            frame["clientProof"], "clientProof", PROOF_BYTES
        ),
    }
    if mode == "start":
        normalised["code"] = _code_field(frame["code"])
    return normalised


def _status_query_frame(frame: Mapping) -> dict:
    _check_keys(
        frame,
        {
            "v",
            "type",
            "channelBinding",
            "requestId",
            "pairingAttemptId",
            "clientProof",
        },
    )
    return {
        "v": FRAME_VERSION,
        "type": "pairing.status",
        "channelBinding": _channel_field(frame["channelBinding"]),
        "requestId": _request_id_field(frame["requestId"]),
        "pairingAttemptId": _wire_b64(
            frame["pairingAttemptId"], "pairingAttemptId", ATTEMPT_BYTES
        ),
        "clientProof": _wire_b64(
            frame["clientProof"], "clientProof", PROOF_BYTES
        ),
    }


def _device_frame(value: object, status: str) -> dict:
    if not isinstance(value, Mapping):
        raise _frame_invalid("device")
    _check_keys(
        value,
        {
            "deviceId",
            "deviceGeneration",
            "targetNodeId",
            "clientPublic",
            "owner",
            "permissions",
            "approvedAt",
            "status",
            "revokedAt",
        },
    )
    device_status = value["status"]
    if device_status not in {"active", "revoked"}:
        raise _frame_invalid("device.status")
    revoked_at = value["revokedAt"]
    if revoked_at is not None:
        revoked_at = _int_field(revoked_at, "device.revokedAt")
    if device_status == "revoked" and revoked_at is None:
        raise _frame_invalid("device.revokedAt")
    if device_status == "active" and revoked_at is not None:
        raise _frame_invalid("device.revokedAt")
    if status == "approved" and device_status != "active":
        raise _frame_invalid("device.status")
    if status == "revoked" and device_status != "revoked":
        raise _frame_invalid("device.status")
    return {
        "deviceId": _identifier_field(value["deviceId"], "deviceId"),
        "deviceGeneration": _int_field(
            value["deviceGeneration"], "deviceGeneration", minimum=1
        ),
        "targetNodeId": _identifier_field(
            value["targetNodeId"], "targetNodeId"
        ),
        "clientPublic": _wire_public(
            value["clientPublic"], "clientPublic"
        ),
        "owner": _text_field(value["owner"], "owner", MAX_OWNER_LENGTH),
        "permissions": _permissions_field(value["permissions"]),
        "approvedAt": _int_field(value["approvedAt"], "device.approvedAt"),
        "status": device_status,
        "revokedAt": revoked_at,
    }


def _status_response_frame(frame: Mapping) -> dict:
    required = {
        "v",
        "type",
        "requestId",
        "nodeId",
        "fingerprint",
        "requestedAt",
        "expiresAt",
        "status",
    }
    _check_keys(
        frame,
        required,
        {"pollAfterMs", "rejectedAt", "device"},
    )
    status = _status_field(frame["status"])
    if status == "pending":
        if "pollAfterMs" not in frame:
            raise _frame_invalid("pollAfterMs")
    elif "pollAfterMs" in frame:
        raise _frame_invalid("pollAfterMs")
    if status == "rejected":
        if "rejectedAt" not in frame:
            raise _frame_invalid("rejectedAt")
    elif "rejectedAt" in frame:
        raise _frame_invalid("rejectedAt")
    if status in {"approved", "revoked"}:
        if "device" not in frame:
            raise _frame_invalid("device")
    elif "device" in frame:
        raise _frame_invalid("device")

    normalised = {
        "v": FRAME_VERSION,
        "type": "pairing.status",
        "requestId": _request_id_field(frame["requestId"]),
        "nodeId": _identifier_field(frame["nodeId"], "nodeId"),
        "fingerprint": _status_fingerprint_field(frame["fingerprint"]),
        "requestedAt": _int_field(frame["requestedAt"], "requestedAt"),
        "expiresAt": _int_field(frame["expiresAt"], "expiresAt"),
        "status": status,
    }
    if status == "pending":
        normalised["pollAfterMs"] = _int_field(
            frame["pollAfterMs"],
            "pollAfterMs",
            minimum=POLL_AFTER_MIN_MS,
            maximum=POLL_AFTER_MAX_MS,
        )
    if status == "rejected":
        normalised["rejectedAt"] = _int_field(
            frame["rejectedAt"], "rejectedAt"
        )
    if status in {"approved", "revoked"}:
        normalised["device"] = _device_frame(frame["device"], status)
    return normalised


def _error_frame(frame: Mapping) -> dict:
    _check_keys(
        frame,
        {"v", "type", "code", "message"},
        {"field", "retryAfterSeconds"},
    )
    code = frame["code"]
    if not isinstance(code, str) or code not in PUBLIC_ERROR_CODES:
        raise _frame_invalid("code")
    message = _text_field(frame["message"], "message", 512)
    field = frame.get("field")
    if field is not None:
        field = _text_field(field, "field", 64)
    retry = frame.get("retryAfterSeconds")
    if code == "pairing-rate-limited":
        if retry is None:
            raise _frame_invalid("retryAfterSeconds")
        retry = _int_field(
            retry, "retryAfterSeconds", minimum=1, maximum=3_600
        )
    elif retry is not None:
        raise _frame_invalid("retryAfterSeconds")
    normalised = {
        "v": FRAME_VERSION,
        "type": "pairing.error",
        "code": code,
        "message": message,
    }
    if field is not None:
        normalised["field"] = field
    if retry is not None:
        normalised["retryAfterSeconds"] = retry
    return normalised


def _normalise_frame(frame: object) -> dict:
    if not isinstance(frame, Mapping):
        raise _frame_invalid()
    version = frame.get("v")
    if type(version) is not int:
        raise _frame_invalid("v")
    if version != FRAME_VERSION:
        raise _protocol_unsupported()
    frame_type = frame.get("type")
    if not isinstance(frame_type, str):
        raise _frame_invalid("type")
    if frame_type == "pairing.hello":
        return _hello_frame(frame)
    if frame_type == "pairing.challenge":
        return _challenge_frame(frame)
    if frame_type == "pairing.redeem":
        return _redeem_frame(frame)
    if frame_type == "pairing.status":
        if "channelBinding" in frame:
            return _status_query_frame(frame)
        if "nodeId" in frame:
            return _status_response_frame(frame)
        raise _frame_invalid()
    if frame_type == "pairing.error":
        return _error_frame(frame)
    raise _frame_invalid("type")


def validate_frame(frame: Mapping) -> dict:
    """Validate and canonicalise an in-memory frame mapping."""

    return _normalise_frame(frame)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _frame_invalid("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise _frame_invalid("non-finite number")


def parse_frame(line: bytes) -> dict:
    """Parse exactly one newline-terminated JSON frame."""

    if not isinstance(line, (bytes, bytearray, memoryview)):
        raise _frame_invalid()
    raw = bytes(line)
    if not raw or len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise _frame_invalid()
    content = raw[:-1]
    if not content or b"\n" in content or content != content.strip():
        raise _frame_invalid()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _frame_invalid() from error
    try:
        frame = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except PairingError:
        raise
    except (json.JSONDecodeError, ValueError) as error:
        raise _frame_invalid() from error
    if not isinstance(frame, dict):
        raise _frame_invalid()
    return _normalise_frame(frame)


def _canonical_bytes(frame: Mapping) -> bytes:
    return json.dumps(
        frame,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def encode_frame(frame: Mapping) -> bytes:
    """Canonicalise and encode a validated frame as one line."""

    normalised = _normalise_frame(frame)
    encoded = _canonical_bytes(normalised) + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise _frame_invalid()
    return encoded


def build_hello(
    *,
    node_id: str,
    host_key_fingerprint: str,
    client_nonce: bytes | str,
    client_public: bytes | str,
) -> dict:
    return _normalise_frame(
        {
            "v": FRAME_VERSION,
            "type": "pairing.hello",
            "protocol": PROTOCOL,
            "nodeId": node_id,
            "hostKeyFingerprint": host_key_fingerprint,
            "clientNonce": client_nonce,
            "clientPublic": client_public,
        }
    )


def build_challenge(
    *,
    node_id: str,
    host_key_fingerprint: str,
    client_nonce: bytes | str,
    client_public: bytes | str,
    host_nonce: bytes | str,
    host_derive_public: bytes | str,
    server_time: int,
    signature: bytes | str,
) -> dict:
    return _normalise_frame(
        {
            "v": FRAME_VERSION,
            "type": "pairing.challenge",
            "protocol": PROTOCOL,
            "nodeId": node_id,
            "hostKeyFingerprint": host_key_fingerprint,
            "clientNonce": client_nonce,
            "clientPublic": client_public,
            "hostNonce": host_nonce,
            "hostDerivePublic": host_derive_public,
            "deriveProfile": DERIVE_PROFILE,
            "serverTime": server_time,
            "signature": signature,
        }
    )


def build_redeem(
    *,
    mode: str,
    channel_binding: str,
    pairing_attempt_id: bytes | str,
    code: str | None,
    device_id: str,
    device_uid: str,
    name: str,
    owner: str,
    client_public: bytes | str,
    permissions: list[str] | tuple[str, ...],
    client_proof: bytes | str,
) -> dict:
    frame = {
        "v": FRAME_VERSION,
        "type": "pairing.redeem",
        "mode": mode,
        "channelBinding": channel_binding,
        "pairingAttemptId": pairing_attempt_id,
        "deviceId": device_id,
        "deviceUid": device_uid,
        "name": name,
        "owner": owner,
        "clientPublic": client_public,
        "permissions": permissions,
        "clientProof": client_proof,
    }
    if code is not None:
        frame["code"] = code
    return _normalise_frame(frame)


def build_status_query(
    *,
    channel_binding: str,
    request_id: str,
    pairing_attempt_id: bytes | str,
    client_proof: bytes | str,
) -> dict:
    return _normalise_frame(
        {
            "v": FRAME_VERSION,
            "type": "pairing.status",
            "channelBinding": channel_binding,
            "requestId": request_id,
            "pairingAttemptId": pairing_attempt_id,
            "clientProof": client_proof,
        }
    )


def build_status_response(
    *,
    request_id: str,
    node_id: str,
    fingerprint: str,
    requested_at: int,
    expires_at: int,
    status: str,
    poll_after_ms: int | None = None,
    rejected_at: int | None = None,
    device: Mapping | None = None,
) -> dict:
    frame = {
        "v": FRAME_VERSION,
        "type": "pairing.status",
        "requestId": request_id,
        "nodeId": node_id,
        "fingerprint": fingerprint,
        "requestedAt": requested_at,
        "expiresAt": expires_at,
        "status": status,
    }
    if poll_after_ms is not None:
        frame["pollAfterMs"] = poll_after_ms
    if rejected_at is not None:
        frame["rejectedAt"] = rejected_at
    if device is not None:
        frame["device"] = device
    return _normalise_frame(frame)


def build_error(
    *,
    code: str,
    message: str,
    field: str | None = None,
    retry_after_seconds: int | None = None,
) -> dict:
    frame = {
        "v": FRAME_VERSION,
        "type": "pairing.error",
        "code": code,
        "message": message,
    }
    if field is not None:
        frame["field"] = field
    if retry_after_seconds is not None:
        frame["retryAfterSeconds"] = retry_after_seconds
    return _normalise_frame(frame)
