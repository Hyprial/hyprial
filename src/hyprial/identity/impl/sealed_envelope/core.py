"""Profile-parameterized sealed envelope: random DEK, per-recipient ephemeral X25519.

Decision (service grant sealed envelope, 2026-11-26): extracted unchanged from the R1 v1
provider-key envelope. HKDF-SHA256 derives each wrapping key from the X25519
shared secret with salt epk || recipient public key and info
wrap_domain || 0 || canonical header || 0 || recipient key id. AES-256-GCM
encrypts the payload under the DEK with the canonical header as AAD, and wraps
the DEK under the derived key with AAD header || 0 || recipient key id. The
order of checks and of entropy draws (DEK, payload nonce, then per recipient an
ephemeral key and a wrap nonce, all through secrets.token_bytes) is part of the
provider-key byte contract and must not change.
"""
from __future__ import annotations

import base64
import json
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..user_key.crypto import canonical_bytes
from ..user_key.encryption.crypto import key_id as encryption_key_id
from ..user_key.models import Error as UserKeyError
from .profile import Profile

_PROBE = X25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _require(profile: Profile, condition: bool, code: str) -> None:
    if not condition:
        raise profile.error(code)


def encode(profile: Profile, value: object) -> bytes:
    """Canonical bounded JSON bytes; refusals use the profile's invalid code."""
    try:
        return canonical_bytes(value)
    except UserKeyError:
        raise profile.error(profile.invalid) from None


def _snapshot(profile: Profile, value: object) -> dict:
    result = json.loads(encode(profile, value))
    _require(profile, type(result) is dict, profile.invalid)
    return result


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(profile: Profile, value: object, size: int | None = None, *, maximum: int = 65536) -> bytes:
    _require(profile, type(value) is str and len(value) <= maximum, profile.invalid)
    try:
        result = base64.b64decode(value, validate=True)
        _require(profile, _b64(result) == value and (size is None or len(result) == size), profile.invalid)
        return result
    except (ValueError, TypeError):
        raise profile.error(profile.invalid) from None


def _fields(profile: Profile, value: object, names: set[str] | frozenset[str]) -> None:
    _require(profile, type(value) is dict and value.keys() == names, profile.invalid)


def _header(profile: Profile, header: object) -> None:
    _fields(profile, header, profile.header_fields)
    profile.check_header(header)


def is_weak_public_key(public: bytes) -> bool:
    """True for a key whose X25519 output is all zero: every low-order point."""
    try:
        return _PROBE.exchange(X25519PublicKey.from_public_bytes(public)) == bytes(32)
    except ValueError:
        return True


def _recipient(profile: Profile, recipient: object) -> None:
    code = profile.argument
    _require(profile, isinstance(recipient, profile.recipient_type), code)
    _require(profile, type(recipient.public_key) is bytes and len(recipient.public_key) == 32, code)
    _require(profile, recipient.key_id == encryption_key_id(recipient.public_key), code)
    if profile.reject_weak_recipients:
        _require(profile, not is_weak_public_key(recipient.public_key), code)


def _kek(profile: Profile, private: X25519PrivateKey, peer: bytes, epk: bytes, recipient: bytes,
         header_bytes: bytes, recipient_id: str) -> bytes:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer))
        _require(profile, shared != bytes(32), profile.unseal)
        info = profile.wrap_domain + b"\0" + header_bytes + b"\0" + recipient_id.encode()
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + recipient, info=info).derive(shared)
    except (ValueError, TypeError):
        raise profile.error(profile.unseal) from None


def seal(profile: Profile, secret: bytes, *, header: dict, recipients, signer=None) -> dict:
    """Seal one payload under a fresh DEK to every recipient; sign when the profile says so."""
    header = _snapshot(profile, header)
    _header(profile, header)
    _require(profile, type(secret) is bytes and 0 < len(secret) <= profile.max_secret_bytes, profile.argument)
    _require(profile, type(recipients) in (list, tuple) and 0 < len(recipients) <= profile.max_recipients,
             profile.argument)
    for recipient in recipients:
        _recipient(profile, recipient)
    _require(profile, len({r.key_id for r in recipients}) == len(recipients), profile.argument)
    if profile.signature is not None:
        profile.signature.check_signer(signer, header)
    else:
        _require(profile, signer is None, profile.argument)
    aad = encode(profile, header)
    dek = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    payload = {"nonce": _b64(nonce), "ciphertext": _b64(AESGCM(dek).encrypt(nonce, secret, aad))}
    wraps = []
    for recipient in recipients:
        ephemeral = X25519PrivateKey.from_private_bytes(secrets.token_bytes(32))
        epk = ephemeral.public_key().public_bytes_raw()
        kek = _kek(profile, ephemeral, recipient.public_key, epk, recipient.public_key, aad, recipient.key_id)
        nonce = secrets.token_bytes(12)
        wrapped = AESGCM(kek).encrypt(nonce, dek, aad + b"\0" + recipient.key_id.encode())
        wraps.append({"recipientKeyId": recipient.key_id, "epk": _b64(epk),
                      "nonce": _b64(nonce), "wrappedDek": _b64(wrapped)})
    result = {"header": header, "recipients": wraps, "payload": payload}
    if profile.signature is not None:
        result = {**result, "sealedBy": profile.signature.sign(result, signer, header)}
    encode(profile, result)
    return result


def verify(profile: Profile, envelope: dict, *, owner_key=None) -> dict:
    """Return a detached, structurally checked (and, if signed, verified) copy."""
    value = _snapshot(profile, envelope)
    top = {"header", "recipients", "payload"}
    _fields(profile, value, top | {"sealedBy"} if profile.signature is not None else top)
    _header(profile, value["header"])
    _fields(profile, value["payload"], {"nonce", "ciphertext"})
    _unb64(profile, value["payload"]["nonce"], 12)
    ciphertext = _unb64(profile, value["payload"]["ciphertext"])
    _require(profile, 16 < len(ciphertext) <= profile.max_secret_bytes + 16, profile.invalid)
    recipients = value["recipients"]
    _require(profile, type(recipients) is list and 0 < len(recipients) <= profile.max_recipients,
             profile.invalid)
    ids = set()
    for row in recipients:
        _fields(profile, row, {"recipientKeyId", "epk", "nonce", "wrappedDek"})
        kid = row["recipientKeyId"]
        _require(profile, type(kid) is str and 0 < len(kid) <= 256 and "\0" not in kid
                 and kid.startswith("x25519:") and len(kid) == 39, profile.invalid)
        _require(profile, all(c in "0123456789abcdef" for c in kid[7:]) and kid not in ids, profile.invalid)
        ids.add(kid)
        _unb64(profile, row["epk"], 32)
        _unb64(profile, row["nonce"], 12)
        _unb64(profile, row["wrappedDek"], 48)
    if profile.signature is not None:
        unsigned = {key: value[key] for key in ("header", "recipients", "payload")}
        profile.signature.verify(unsigned, value["sealedBy"], owner_key, value["header"])
    else:
        _require(profile, owner_key is None, profile.argument)
    return value


def _unwrap(profile: Profile, row: dict, private_key: bytes, header_bytes: bytes) -> bytes:
    _require(profile, type(private_key) is bytes and len(private_key) == 32, profile.unseal)
    key = X25519PrivateKey.from_private_bytes(private_key)
    public = key.public_key().public_bytes_raw()
    _require(profile, encryption_key_id(public) == row["recipientKeyId"], profile.unseal)
    epk = _unb64(profile, row["epk"], 32)
    kek = _kek(profile, key, epk, epk, public, header_bytes, row["recipientKeyId"])
    try:
        dek = AESGCM(kek).decrypt(_unb64(profile, row["nonce"], 12), _unb64(profile, row["wrappedDek"], 48),
                                  header_bytes + b"\0" + row["recipientKeyId"].encode())
        _require(profile, len(dek) == 32, profile.unseal)
        return dek
    except (InvalidTag, ValueError):
        raise profile.error(profile.unseal) from None


def _row(profile: Profile, value: dict, recipient_key_id: str) -> dict:
    row = next((r for r in value["recipients"] if r["recipientKeyId"] == recipient_key_id), None)
    _require(profile, row is not None, profile.missing)
    return row


def open(profile: Profile, envelope: dict, *, recipient_key_id: str, private_key: bytes,
         owner_key=None) -> bytes:
    """Verify before key exchange and decryption. This is not grant authorization."""
    value = verify(profile, envelope, owner_key=owner_key)
    row = _row(profile, value, recipient_key_id)
    aad = encode(profile, value["header"])
    dek = _unwrap(profile, row, private_key, aad)
    try:
        return AESGCM(dek).decrypt(_unb64(profile, value["payload"]["nonce"], 12),
                                   _unb64(profile, value["payload"]["ciphertext"]), aad)
    except (InvalidTag, ValueError):
        raise profile.error(profile.unseal) from None


def unwrap_check(profile: Profile, envelope: dict, *, recipient_key_id: str, private_key: bytes,
                 owner_key=None) -> None:
    """Verify, then unwrap only this recipient's DEK; the payload is never decrypted."""
    value = verify(profile, envelope, owner_key=owner_key)
    row = _row(profile, value, recipient_key_id)
    _unwrap(profile, row, private_key, encode(profile, value["header"]))
