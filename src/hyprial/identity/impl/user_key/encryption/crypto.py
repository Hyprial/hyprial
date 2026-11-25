"""Independent X25519 generation/exchange; no Ed25519 conversion or KDF policy."""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from hyprial.kernel import ipc_errors
from ..crypto import decode
from ..models import Error


def key_id(public: bytes) -> str:
    return "x25519:" + hashlib.sha256(public).hexdigest()[:32]


def generate() -> tuple[str, str, str]:
    key = X25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    return (key_id(public), base64.b64encode(public).decode(),
            base64.b64encode(key.private_bytes_raw()).decode())


def validate_pair(public_key: str, private_key: str | None, kid: str) -> None:
    public = decode(public_key, 32)
    if kid != key_id(public):
        raise Error(ipc_errors.USER_KEY_QUARANTINED)
    if private_key is not None:
        key = X25519PrivateKey.from_private_bytes(decode(private_key, 32))
        if key.public_key().public_bytes_raw() != public:
            raise Error(ipc_errors.USER_KEY_QUARANTINED)


def exchange(private_key: str, peer_public_key: str) -> bytes:
    """Daemon-memory-only shared secret. Not a DEK or provider wrap format."""
    try:
        key = X25519PrivateKey.from_private_bytes(decode(private_key, 32))
        return key.exchange(X25519PublicKey.from_public_bytes(decode(peer_public_key, 32)))
    except (ValueError, TypeError, Error):
        # Includes low-order / all-zero shared secrets. Never echo input bytes.
        raise Error(ipc_errors.PAYLOAD_INVALID) from None
