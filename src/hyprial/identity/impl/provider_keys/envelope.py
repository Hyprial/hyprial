"""R1 v1: independent random DEK, per-recipient ephemeral X25519, owner signature.

Decision (service grant sealed envelope, 2026-11-26): the construction moved to the
sealed_envelope primitive; this module is the provider-key profile binding.
Wire bytes, check order and refusal codes are unchanged: unusable seal input
and crypto failures stay PROVIDER_KEY_UNSEAL_FAILED, structure and signature
faults stay PROVIDER_KEY_SIGNATURE_INVALID, and a missing row stays
PROVIDER_KEY_RECIPIENT_PENDING.
"""
from __future__ import annotations

from hyprial.kernel import ipc_errors
from ..sealed_envelope import Profile, Signature
from ..sealed_envelope import core as sealed_core
from .models import (
    ENVELOPE_PURPOSE, INVALID, MAX_RECIPIENTS, MAX_SECRET_BYTES, UNSEAL, WRAP_DOMAIN,
    Error, OwnerKey, Recipient, SigningKey, integer, require, sign, validate_header, verify,
)


def _check_signer(signer: object, header: dict) -> None:
    require(isinstance(signer, SigningKey) and signer.owner == header["owner"])


def _sign(unsigned: dict, signer: SigningKey, header: dict) -> dict:
    return sign(unsigned, signer, ENVELOPE_PURPOSE, owner=header["owner"])


def _verify(unsigned: dict, sealed_by: object, owner_key: object, header: dict) -> None:
    verify(unsigned, sealed_by, owner_key, ENVELOPE_PURPOSE, owner=header["owner"])


PROFILE = Profile(
    name="provider-key", wrap_domain=WRAP_DOMAIN,
    header_fields=frozenset({"v", "alg", "wrap", "provider", "keyId", "owner", "dekVersion", "createdAt"}),
    check_header=validate_header,
    signature=Signature(check_signer=_check_signer, sign=_sign, verify=_verify),
    error=Error, invalid=INVALID, unseal=UNSEAL, missing=ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING,
    argument=UNSEAL, recipient_type=Recipient, max_secret_bytes=MAX_SECRET_BYTES,
    max_recipients=MAX_RECIPIENTS, reject_weak_recipients=False,
)


def seal(secret: bytes, *, header: dict, recipients: list[Recipient], signer: SigningKey) -> dict:
    """Seal one payload with a fresh DEK. Use reseal for an existing key version.

    Callers own cross-record version allocation. This pure primitive cannot prove
    that a caller did not reuse a version in another process or storage replica.
    """
    return sealed_core.seal(PROFILE, secret, header=header, recipients=recipients, signer=signer)


def verify_envelope(envelope: dict, *, owner_key: OwnerKey) -> dict:
    """Return a detached verified object. No key is accepted from this document."""
    return sealed_core.verify(PROFILE, envelope, owner_key=owner_key)


def open_envelope(envelope: dict, *, owner_key: OwnerKey, recipient_key_id: str,
                  private_key: bytes) -> bytes:
    """Verify before key exchange/decryption. This is not grant authorization."""
    return sealed_core.open(PROFILE, envelope, owner_key=owner_key, recipient_key_id=recipient_key_id,
                            private_key=private_key)


def unwrap_check(envelope: dict, *, owner_key: OwnerKey, recipient_key_id: str,
                 private_key: bytes) -> None:
    """Verify, then unwrap only this recipient's DEK; the payload is never decrypted.

    Refusals match open_envelope: a missing row is RECIPIENT_PENDING and a wrong
    key or altered wrap is UNSEAL. Success proves custody of the wrap, not that
    the payload ciphertext opens.
    """
    sealed_core.unwrap_check(PROFILE, envelope, owner_key=owner_key, recipient_key_id=recipient_key_id,
                             private_key=private_key)


def reseal(envelope: dict, *, owner_key: OwnerKey, recipient_key_id: str,
           private_key: bytes, signer: SigningKey, recipients: list[Recipient], created_at: int) -> dict:
    """Re-seal the verified plaintext, incrementing its version and replacing DEK."""
    value = verify_envelope(envelope, owner_key=owner_key)
    secret = open_envelope(value, owner_key=owner_key, recipient_key_id=recipient_key_id, private_key=private_key)
    header = value["header"]
    require(integer(created_at) and created_at >= header["createdAt"])
    header = {**header, "dekVersion": header["dekVersion"] + 1, "createdAt": created_at}
    return seal(secret, header=header, recipients=recipients, signer=signer)
