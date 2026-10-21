"""P-256 primitives for the S1 pairing protocol.

The wire representation is deliberately narrow: uncompressed P-256 public
keys and raw ``r||s`` ECDSA signatures, both base64url without padding.  The
request key is the HKDF-SHA256 result of the fixed v1 profile.  These helpers
contain no storage, listener or transport code and never log key material.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

P256 = ec.SECP256R1()
PUBLIC_KEY_BYTES = 65
NONCE_BYTES = 32
SIGNATURE_BYTES = 64
PROOF_BYTES = 32
REQUEST_KEY_BYTES = 32
_UNCOMPRESSED_PREFIX = b"\x04"

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_DERIVE_SALT = hashlib.sha256(b"hyprial/m11/derive-salt/v1").digest()
_DERIVE_INFO_PREFIX = b"hyprial/m11/derive-key/v1"


class CryptoError(ValueError):
    """A named cryptographic shape or verification refusal.

    Messages never include private keys, request keys or raw signatures.
    """


def b64url_encode(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CryptoError("base64url input must be bytes")
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not _B64URL.fullmatch(value):
        raise CryptoError("invalid base64url encoding")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as error:
        raise CryptoError("invalid base64url encoding") from error
    if b64url_encode(decoded) != value:
        raise CryptoError("non-canonical base64url encoding")
    return decoded


def _require_p256_private(key: object) -> ec.EllipticCurvePrivateKey:
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != (
        "secp256r1"
    ):
        raise CryptoError("P-256 private key required")
    return key


def _require_p256_public(key: object) -> ec.EllipticCurvePublicKey:
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != (
        "secp256r1"
    ):
        raise CryptoError("P-256 public key required")
    return key


def public_key_raw(key: ec.EllipticCurvePublicKey) -> bytes:
    public = _require_p256_public(key)
    return public.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def decode_public_key(value: str) -> ec.EllipticCurvePublicKey:
    raw = b64url_decode(value)
    if len(raw) != PUBLIC_KEY_BYTES or raw[:1] != _UNCOMPRESSED_PREFIX:
        raise CryptoError("P-256 uncompressed public key required")
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(P256, raw)
    except ValueError as error:
        raise CryptoError("public key is not on P-256") from error
    return _require_p256_public(key)


def encode_public_key(key: ec.EllipticCurvePublicKey) -> str:
    return b64url_encode(public_key_raw(key))


def host_key_fingerprint(spki: bytes) -> str:
    """The operator pin for a host SPKI, independent of TLS plumbing."""

    if not isinstance(spki, (bytes, bytearray, memoryview)):
        raise CryptoError("SPKI must be bytes")
    return "sha256:" + hashlib.sha256(bytes(spki)).hexdigest()


def sign_challenge(
    private_key: ec.EllipticCurvePrivateKey, transcript: bytes
) -> str:
    key = _require_p256_private(private_key)
    if not isinstance(transcript, (bytes, bytearray, memoryview)):
        raise CryptoError("transcript must be bytes")
    der = key.sign(bytes(transcript), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return b64url_encode(raw)


def verify_challenge(
    public_key: ec.EllipticCurvePublicKey,
    transcript: bytes,
    signature: str,
) -> None:
    key = _require_p256_public(public_key)
    if not isinstance(transcript, (bytes, bytearray, memoryview)):
        raise CryptoError("transcript must be bytes")
    raw = b64url_decode(signature)
    if len(raw) != SIGNATURE_BYTES:
        raise CryptoError("challenge signature must be 64 bytes")
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    if r == 0 or s == 0:
        raise CryptoError("challenge signature is malformed")
    try:
        key.verify(
            encode_dss_signature(r, s),
            bytes(transcript),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature as error:
        raise CryptoError("challenge signature is invalid") from error


def derive_request_key(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    peer_public: str,
    host_derive_public: str,
    client_public: str,
) -> bytes:
    """Derive the v1 request key from one's private key and both public keys.

    ``peer_public`` is the other party's ephemeral derivation key: the host
    passes the client public key and the client passes the host derivation
    public key.  The HKDF info binds both public keys in host/client order.
    """

    key = _require_p256_private(private_key)
    peer = decode_public_key(peer_public)
    host_public = decode_public_key(host_derive_public)
    client_public_key = decode_public_key(client_public)
    host_raw = public_key_raw(host_public)
    client_raw = public_key_raw(client_public_key)
    try:
        shared = key.exchange(ec.ECDH(), peer)
    except (ValueError, TypeError) as error:
        raise CryptoError("ECDH agreement failed") from error
    if len(shared) != 32:
        raise CryptoError("unexpected ECDH output length")
    info = (
        _DERIVE_INFO_PREFIX
        + b"\x00"
        + host_raw
        + b"\x00"
        + client_raw
    )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=REQUEST_KEY_BYTES,
        salt=_DERIVE_SALT,
        info=info,
    ).derive(shared)


def hmac_sha256(key: bytes | bytearray, message: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise CryptoError("HMAC key must be bytes")
    if not isinstance(message, (bytes, bytearray, memoryview)):
        raise CryptoError("HMAC message must be bytes")
    return hmac.new(bytes(key), bytes(message), hashlib.sha256).digest()
