"""Domain-separated canonical transcripts and HMAC client proofs.

The pairing pre-images are deliberately different from the N-A2 business MAC
in ``adapter/envelope.py`` and the Rust signer.  A valid pairing proof can
therefore never be replayed as a business request (or the other way round).
"""

from __future__ import annotations

import hashlib
import hmac
import json

from ..models import normalize_pairing_code
from .crypto import CryptoError, b64url_decode, b64url_encode, hmac_sha256
from .protocol import DERIVE_PROFILE, PROTOCOL, PairingError

CHALLENGE_PURPOSE = "m11-pairing-challenge-v1"
REDEEM_PURPOSE = "m11-pairing-redeem-v1"
RESUME_PURPOSE = "m11-pairing-resume-v1"
STATUS_PURPOSE = "m11-pairing-status-v1"

_REQUEST_KEY_BYTES = 32


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def code_hash(code: str) -> str:
    normalised = normalize_pairing_code(code)
    return "sha256:" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def channel_binding(challenge_transcript_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(
        challenge_transcript_bytes
    ).hexdigest()


def challenge_transcript(
    *,
    node_id: str,
    host_key_fingerprint: str,
    client_nonce: str,
    host_nonce: str,
    client_public: str,
    host_derive_public: str,
    server_time: int,
) -> bytes:
    return canonical_json(
        {
            "purpose": CHALLENGE_PURPOSE,
            "v": 1,
            "protocol": PROTOCOL,
            "nodeId": node_id,
            "hostKeyFingerprint": host_key_fingerprint,
            "clientNonce": client_nonce,
            "hostNonce": host_nonce,
            "clientPublic": client_public,
            "hostDerivePublic": host_derive_public,
            "deriveProfile": DERIVE_PROFILE,
            "serverTime": server_time,
        }
    )


def redeem_transcript(
    *,
    mode: str,
    channel_binding: str,
    pairing_attempt_id: str,
    code: str | None,
    device_id: str,
    device_uid: str,
    name: str,
    owner: str,
    client_public: str,
    permissions: list[str] | tuple[str, ...],
) -> bytes:
    if mode == "start":
        if code is None:
            raise PairingError(
                "pairing-frame-invalid", "pairing frame is invalid", field="code"
            )
        purpose = REDEEM_PURPOSE
        preimage: dict = {"codeHash": code_hash(code)}
    elif mode == "resume":
        if code is not None:
            raise PairingError(
                "pairing-frame-invalid", "pairing frame is invalid", field="code"
            )
        purpose = RESUME_PURPOSE
        preimage = {}
    else:
        raise PairingError(
            "pairing-frame-invalid", "pairing frame is invalid", field="mode"
        )
    preimage.update(
        {
            "purpose": purpose,
            "mode": mode,
            "channelBinding": channel_binding,
            "pairingAttemptId": pairing_attempt_id,
            "deviceId": device_id,
            "deviceUid": device_uid,
            "name": name,
            "owner": owner,
            "clientPublic": client_public,
            "permissions": sorted(set(permissions)),
        }
    )
    return canonical_json(preimage)


def status_transcript(
    *,
    channel_binding: str,
    request_id: str,
    pairing_attempt_id: str,
) -> bytes:
    return canonical_json(
        {
            "purpose": STATUS_PURPOSE,
            "channelBinding": channel_binding,
            "requestId": request_id,
            "pairingAttemptId": pairing_attempt_id,
        }
    )


def _require_request_key(request_key: bytes | bytearray) -> bytes:
    if not isinstance(request_key, (bytes, bytearray, memoryview)):
        raise PairingError(
            "pairing-unavailable", "pairing is unavailable", close=True
        )
    key = bytes(request_key)
    if len(key) != _REQUEST_KEY_BYTES:
        raise PairingError(
            "pairing-unavailable", "pairing is unavailable", close=True
        )
    return key


def client_proof(
    request_key: bytes | bytearray,
    transcript: bytes,
) -> str:
    key = _require_request_key(request_key)
    if not isinstance(transcript, (bytes, bytearray, memoryview)):
        raise PairingError(
            "pairing-client-proof-invalid",
            "pairing proof is invalid",
            close=True,
        )
    try:
        proof = hmac_sha256(key, bytes(transcript))
    except CryptoError as error:
        raise PairingError(
            "pairing-client-proof-invalid",
            "pairing proof is invalid",
            close=True,
        ) from error
    return b64url_encode(proof)


def verify_client_proof(
    request_key: bytes | bytearray,
    transcript: bytes,
    proof: str,
) -> None:
    key = _require_request_key(request_key)
    if not isinstance(transcript, (bytes, bytearray, memoryview)):
        raise PairingError(
            "pairing-client-proof-invalid",
            "pairing proof is invalid",
            close=True,
        )
    try:
        provided = b64url_decode(proof)
    except CryptoError as error:
        raise PairingError(
            "pairing-client-proof-invalid",
            "pairing proof is invalid",
            close=True,
        ) from error
    if len(provided) != 32:
        raise PairingError(
            "pairing-client-proof-invalid",
            "pairing proof is invalid",
            close=True,
        )
    expected = hmac_sha256(key, bytes(transcript))
    if not hmac.compare_digest(expected, provided):
        raise PairingError(
            "pairing-client-proof-invalid",
            "pairing proof is invalid",
            close=True,
        )
