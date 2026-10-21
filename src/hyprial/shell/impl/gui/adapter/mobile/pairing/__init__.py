"""S1 core-only M11 pairing protocol and pure state machine.

The framing/transcript/crypto/state surface below stays independent of
transport and storage.  The opt-in S3 loopback listener lives behind the
sibling :mod:`...pairing.server` subpackage; importing this package neither
imports nor starts that server.  Persistence, operator approval commands,
device lifecycle, host.py exposure and Android/iOS integration remain outside
this package.
"""

from __future__ import annotations

from . import protocol
from .authority import AuthorityError, PairingAuthority
from .crypto import (
    CryptoError,
    b64url_decode,
    b64url_encode,
    decode_public_key,
    derive_request_key,
    encode_public_key,
    host_key_fingerprint,
    hmac_sha256,
    public_key_raw,
    sign_challenge,
    verify_challenge,
)
from .protocol import (
    APPROVED,
    DERIVE_PROFILE,
    EXPIRED,
    MAX_FRAME_BYTES,
    PENDING,
    PROTOCOL,
    REJECTED,
    REVOKED,
    PairingError,
    authority_refusal,
    build_challenge,
    build_error,
    build_hello,
    build_redeem,
    build_status_query,
    build_status_response,
    encode_frame,
    normalize_pairing_code,
    parse_frame,
    validate_frame,
)
from .state import (
    ClientPairingState,
    HostPairingState,
    pairing_grants,
    project_status,
)
from .transcript import (
    canonical_json,
    challenge_transcript,
    channel_binding,
    client_proof,
    code_hash,
    redeem_transcript,
    status_transcript,
    verify_client_proof,
)

__all__ = [
    "APPROVED",
    "AuthorityError",
    "ClientPairingState",
    "CryptoError",
    "DERIVE_PROFILE",
    "EXPIRED",
    "HostPairingState",
    "MAX_FRAME_BYTES",
    "PENDING",
    "PROTOCOL",
    "PairingAuthority",
    "PairingError",
    "REJECTED",
    "REVOKED",
    "authority_refusal",
    "b64url_decode",
    "b64url_encode",
    "build_challenge",
    "build_error",
    "build_hello",
    "build_redeem",
    "build_status_query",
    "build_status_response",
    "canonical_json",
    "challenge_transcript",
    "channel_binding",
    "client_proof",
    "code_hash",
    "decode_public_key",
    "derive_request_key",
    "encode_frame",
    "encode_public_key",
    "hmac_sha256",
    "host_key_fingerprint",
    "normalize_pairing_code",
    "pairing_grants",
    "parse_frame",
    "project_status",
    "protocol",
    "public_key_raw",
    "redeem_transcript",
    "sign_challenge",
    "status_transcript",
    "validate_frame",
    "verify_challenge",
    "verify_client_proof",
]
