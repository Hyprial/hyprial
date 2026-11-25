"""Pure provider-key types and strict R1 v1 validation. No trust discovery or I/O."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from hyprial.kernel import DaemonRequestError, ipc_errors
from ..user_key.crypto import canonical_bytes, key_id as signing_key_id
from ..user_key.models import Error as UserKeyError
from ..user_key.encryption.crypto import key_id as encryption_key_id

ENVELOPE_PURPOSE = b"hyprial/provider-key/envelope/v1"
GRANT_PURPOSE = b"hyprial/provider-key/grant/v1"
RECORD_PURPOSE = b"hyprial/provider-key/record/v1"
WRAP_DOMAIN = b"hyprial/provider-key/wrap/v1"
MAX_SECRET_BYTES = 16384
MAX_RECIPIENTS = 32
MAX_INTEGER = 9007199254740991
INVALID = ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
UNSEAL = ipc_errors.PROVIDER_KEY_UNSEAL_FAILED


class Error(DaemonRequestError):
    """Public, value-free failures: never retain parser or cryptography messages."""

    def __init__(self, code: str):
        messages = {
            ipc_errors.PROVIDER_GRANT_MISSING: "provider grant missing",
            ipc_errors.PROVIDER_GRANT_REVOKED: "provider grant revoked",
            ipc_errors.PROVIDER_KEY_NOT_FOUND: "provider key not found",
            ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING: "provider key recipient unavailable",
            UNSEAL: "provider key unseal failed",
            INVALID: "provider key signature or signed structure invalid",
            ipc_errors.PROVIDER_STORE_UNAVAILABLE: "provider-key owner store unavailable",
            ipc_errors.PROVIDER_STORE_CONFLICT: "provider-key owner store write conflict",
        }
        super().__init__(code, messages.get(code, "provider key operation refused"))


@dataclass(frozen=True)
class SigningKey:
    """In-memory primitive input, not a daemon signing capability or key store."""
    owner: str
    private_key: bytes = field(repr=False)


@dataclass(frozen=True)
class OwnerKey:
    """Caller-supplied verified U2 anchor. A self-reported public key is not trust."""
    owner: str
    key_id: str
    public_key: bytes
    key_kind: str = "owner"


@dataclass(frozen=True)
class Recipient:
    """Caller-resolved X1 public key; no host/user ownership discovery here."""
    key_id: str
    public_key: bytes


def require(condition: bool, code: str = INVALID) -> None:
    if not condition:
        raise Error(code)


def text(value: object, maximum: int = 256) -> bool:
    return type(value) is str and 0 < len(value) <= maximum and "\0" not in value


def integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= MAX_INTEGER


def object_fields(value: object, required: set[str], optional: set[str] = frozenset()) -> None:
    require(type(value) is dict and required <= value.keys() <= required | optional)


def encode(value: object) -> bytes:
    try:
        return canonical_bytes(value)
    except UserKeyError:
        raise Error(INVALID) from None


def snapshot(value: object) -> dict:
    """Bound input and detach it before validation/verification (no mutable views)."""
    result = json.loads(encode(value))
    require(type(result) is dict)
    return result


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def unb64(value: object, size: int | None = None, *, maximum: int = 65536) -> bytes:
    require(type(value) is str and len(value) <= maximum)
    try:
        result = base64.b64decode(value, validate=True)
        require(b64(result) == value and (size is None or len(result) == size))
        return result
    except (ValueError, TypeError):
        raise Error(INVALID) from None


def validate_header(header: object) -> None:
    object_fields(header, {"v", "alg", "wrap", "provider", "keyId", "owner", "dekVersion", "createdAt"})
    require(type(header["v"]) is int and header["v"] == 1)
    require(header["alg"] == "A256GCM" and header["wrap"] == "X25519-HKDF-SHA256-A256GCM")
    require(all(text(header[name]) for name in ("provider", "keyId", "owner")))
    require(integer(header["dekVersion"], 1) and integer(header["createdAt"]))


def validate_recipient(recipient: Recipient) -> None:
    require(isinstance(recipient, Recipient), UNSEAL)
    require(type(recipient.public_key) is bytes and len(recipient.public_key) == 32, UNSEAL)
    require(recipient.key_id == encryption_key_id(recipient.public_key), UNSEAL)


def signed_input(purpose: bytes, value: dict) -> bytes:
    require(purpose in (ENVELOPE_PURPOSE, GRANT_PURPOSE, RECORD_PURPOSE))
    return purpose + b"\0" + hashlib.sha256(encode(value)).digest()


def sign(value: dict, signer: SigningKey, purpose: bytes, *, owner: str) -> dict:
    require(isinstance(signer, SigningKey) and signer.owner == owner)
    require(type(signer.private_key) is bytes and len(signer.private_key) == 32)
    key = Ed25519PrivateKey.from_private_bytes(signer.private_key)
    return {"keyId": signing_key_id(key.public_key().public_bytes_raw()),
            "sig": b64(key.sign(signed_input(purpose, value)))}


def verify(value: dict, sealed_by: dict, owner_key: OwnerKey, purpose: bytes, *, owner: str) -> None:
    object_fields(sealed_by, {"keyId", "sig"})
    require(isinstance(owner_key, OwnerKey) and owner_key.key_kind == "owner" and owner_key.owner == owner)
    require(type(owner_key.public_key) is bytes and len(owner_key.public_key) == 32)
    require(sealed_by["keyId"] == owner_key.key_id == signing_key_id(owner_key.public_key))
    signature = unb64(sealed_by["sig"], 64)
    try:
        Ed25519PublicKey.from_public_bytes(owner_key.public_key).verify(signature, signed_input(purpose, value))
    except (InvalidSignature, ValueError):
        raise Error(INVALID) from None
