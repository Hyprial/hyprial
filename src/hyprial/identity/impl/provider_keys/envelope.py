"""R1 v1: independent random DEK, per-recipient ephemeral X25519, owner signature."""
from __future__ import annotations

import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hyprial.kernel import ipc_errors
from ..user_key.encryption.crypto import key_id as encryption_key_id
from .models import (
    ENVELOPE_PURPOSE, MAX_RECIPIENTS, MAX_SECRET_BYTES, UNSEAL, WRAP_DOMAIN,
    Error, OwnerKey, Recipient, SigningKey, b64, encode, integer, object_fields,
    require, sign, snapshot, text, unb64, validate_header, validate_recipient, verify,
)


def _kek(private: X25519PrivateKey, peer: bytes, epk: bytes, recipient: bytes,
         header_bytes: bytes, recipient_id: str) -> bytes:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer))
        require(shared != bytes(32), UNSEAL)
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + recipient,
                    info=WRAP_DOMAIN + b"\0" + header_bytes + b"\0" + recipient_id.encode()).derive(shared)
    except (ValueError, TypeError):
        raise Error(UNSEAL) from None


def seal(secret: bytes, *, header: dict, recipients: list[Recipient], signer: SigningKey) -> dict:
    """Seal one payload with a fresh DEK. Use reseal for an existing key version.

    Callers own cross-record version allocation. This pure primitive cannot prove
    that a caller did not reuse a version in another process or storage replica.
    """
    header = snapshot(header)
    validate_header(header)
    require(type(secret) is bytes and 0 < len(secret) <= MAX_SECRET_BYTES, UNSEAL)
    require(type(recipients) in (list, tuple) and 0 < len(recipients) <= MAX_RECIPIENTS, UNSEAL)
    for recipient in recipients:
        validate_recipient(recipient)
    require(len({r.key_id for r in recipients}) == len(recipients), UNSEAL)
    require(isinstance(signer, SigningKey) and signer.owner == header["owner"])
    aad = encode(header)
    dek = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    payload = {"nonce": b64(nonce), "ciphertext": b64(AESGCM(dek).encrypt(nonce, secret, aad))}
    wraps = []
    for recipient in recipients:
        ephemeral = X25519PrivateKey.from_private_bytes(secrets.token_bytes(32))
        epk = ephemeral.public_key().public_bytes_raw()
        kek = _kek(ephemeral, recipient.public_key, epk, recipient.public_key, aad, recipient.key_id)
        nonce = secrets.token_bytes(12)
        wrapped = AESGCM(kek).encrypt(nonce, dek, aad + b"\0" + recipient.key_id.encode())
        wraps.append({"recipientKeyId": recipient.key_id, "epk": b64(epk),
                      "nonce": b64(nonce), "wrappedDek": b64(wrapped)})
    unsigned = {"header": header, "recipients": wraps, "payload": payload}
    result = {**unsigned, "sealedBy": sign(unsigned, signer, ENVELOPE_PURPOSE, owner=header["owner"])}
    encode(result)  # The complete signed object also respects U1's bound.
    return result


def verify_envelope(envelope: dict, *, owner_key: OwnerKey) -> dict:
    """Return a detached verified object. No key is accepted from this document."""
    value = snapshot(envelope)
    object_fields(value, {"header", "recipients", "payload", "sealedBy"})
    validate_header(value["header"])
    object_fields(value["payload"], {"nonce", "ciphertext"})
    unb64(value["payload"]["nonce"], 12)
    ciphertext = unb64(value["payload"]["ciphertext"])
    require(16 < len(ciphertext) <= MAX_SECRET_BYTES + 16)
    recipients = value["recipients"]
    require(type(recipients) is list and 0 < len(recipients) <= MAX_RECIPIENTS)
    ids = set()
    for row in recipients:
        object_fields(row, {"recipientKeyId", "epk", "nonce", "wrappedDek"})
        kid = row["recipientKeyId"]
        require(text(kid) and kid.startswith("x25519:") and len(kid) == 39)
        require(all(c in "0123456789abcdef" for c in kid[7:]) and kid not in ids)
        ids.add(kid)
        unb64(row["epk"], 32)
        unb64(row["nonce"], 12)
        unb64(row["wrappedDek"], 48)
    unsigned = {key: value[key] for key in ("header", "recipients", "payload")}
    verify(unsigned, value["sealedBy"], owner_key, ENVELOPE_PURPOSE, owner=value["header"]["owner"])
    return value


def _unwrap(row: dict, private_key: bytes, header_bytes: bytes) -> bytes:
    require(type(private_key) is bytes and len(private_key) == 32, UNSEAL)
    key = X25519PrivateKey.from_private_bytes(private_key)
    public = key.public_key().public_bytes_raw()
    require(encryption_key_id(public) == row["recipientKeyId"], UNSEAL)
    epk = unb64(row["epk"], 32)
    kek = _kek(key, epk, epk, public, header_bytes, row["recipientKeyId"])
    try:
        dek = AESGCM(kek).decrypt(unb64(row["nonce"], 12), unb64(row["wrappedDek"], 48),
                                  header_bytes + b"\0" + row["recipientKeyId"].encode())
        require(len(dek) == 32, UNSEAL)
        return dek
    except (InvalidTag, ValueError):
        raise Error(UNSEAL) from None


def open_envelope(envelope: dict, *, owner_key: OwnerKey, recipient_key_id: str,
                  private_key: bytes) -> bytes:
    """Verify before key exchange/decryption. This is not grant authorization."""
    value = verify_envelope(envelope, owner_key=owner_key)
    row = next((r for r in value["recipients"] if r["recipientKeyId"] == recipient_key_id), None)
    require(row is not None, ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING)
    aad = encode(value["header"])
    dek = _unwrap(row, private_key, aad)
    try:
        return AESGCM(dek).decrypt(unb64(value["payload"]["nonce"], 12),
                                    unb64(value["payload"]["ciphertext"]), aad)
    except (InvalidTag, ValueError):
        raise Error(UNSEAL) from None


def unwrap_check(envelope: dict, *, owner_key: OwnerKey, recipient_key_id: str,
                 private_key: bytes) -> None:
    """Verify, then unwrap only this recipient's DEK; the payload is never decrypted.

    Refusals match open_envelope: a missing row is RECIPIENT_PENDING and a wrong
    key or altered wrap is UNSEAL. Success proves custody of the wrap, not that
    the payload ciphertext opens.
    """
    value = verify_envelope(envelope, owner_key=owner_key)
    row = next((r for r in value["recipients"] if r["recipientKeyId"] == recipient_key_id), None)
    require(row is not None, ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING)
    _unwrap(row, private_key, encode(value["header"]))


def reseal(envelope: dict, *, owner_key: OwnerKey, recipient_key_id: str,
           private_key: bytes, signer: SigningKey, recipients: list[Recipient], created_at: int) -> dict:
    """Re-seal the verified plaintext, incrementing its version and replacing DEK."""
    value = verify_envelope(envelope, owner_key=owner_key)
    secret = open_envelope(value, owner_key=owner_key, recipient_key_id=recipient_key_id, private_key=private_key)
    header = value["header"]
    require(integer(created_at) and created_at >= header["createdAt"])
    header = {**header, "dekVersion": header["dekVersion"] + 1, "createdAt": created_at}
    return seal(secret, header=header, recipients=recipients, signer=signer)
