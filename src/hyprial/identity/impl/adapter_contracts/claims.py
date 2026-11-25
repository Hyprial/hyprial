"""Closed signed adapter claim wire values owned by identity."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
from dataclasses import asdict, dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ClaimReceiptV1:
    receipt_id: str
    action: str
    organization_id: str
    instance_id: str
    instance_generation: int
    agent: str
    agent_entity_token: str
    association_generation: int
    claim_generation: int
    registrar_epoch: int
    operation_id: str
    request_nonce: str
    request_digest: str
    effect_digest: str
    issued_at_ms: int
    expires_at_ms: int
    signer_public_key_b64: str
    result_digest: str
    signature_b64: str

    def to_bytes(self) -> bytes:
        return canonical_bytes(asdict(self))

    @classmethod
    def from_bytes(cls, payload: bytes) -> ClaimReceiptV1:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("CLAIM_SIGNATURE_INVALID") from error
        if not isinstance(value, dict) or set(value) != {
            field.name for field in dataclasses.fields(cls)
        }:
            raise ValueError("CLAIM_SIGNATURE_INVALID")
        try:
            return cls(**value)
        except TypeError as error:
            raise ValueError("CLAIM_SIGNATURE_INVALID") from error

    def unsigned(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("result_digest")
        value.pop("signature_b64")
        return value

    def verify(self, *, now_ms: int) -> None:
        body = canonical_bytes(self.unsigned())
        digest = hashlib.sha256(b"hyprial-claim-receipt-v1\0" + body).hexdigest()
        if digest != self.result_digest:
            raise ValueError("CLAIM_SIGNATURE_INVALID")
        if (
            now_ms >= self.expires_at_ms
            or self.expires_at_ms - self.issued_at_ms > 60_000
        ):
            raise ValueError("CLAIM_SIGNATURE_INVALID")
        public = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(self.signer_public_key_b64, validate=True)
        )
        public.verify(
            base64.b64decode(self.signature_b64, validate=True),
            b"hyprial-claim-receipt-v1\0" + body,
        )


def sign_claim_receipt(
    private_key: Ed25519PrivateKey, **fields: object
) -> ClaimReceiptV1:
    public = private_key.public_key().public_bytes_raw()
    unsigned = ClaimReceiptV1(
        **fields,  # type: ignore[arg-type]
        signer_public_key_b64=base64.b64encode(public).decode("ascii"),
        result_digest="",
        signature_b64="",
    )
    body = canonical_bytes(unsigned.unsigned())
    digest = hashlib.sha256(b"hyprial-claim-receipt-v1\0" + body).hexdigest()
    signature = private_key.sign(b"hyprial-claim-receipt-v1\0" + body)
    return ClaimReceiptV1(
        **unsigned.unsigned(),  # type: ignore[arg-type]
        result_digest=digest,
        signature_b64=base64.b64encode(signature).decode("ascii"),
    )


@dataclass(frozen=True, slots=True)
class AdapterClaimResult:
    receipt_id: str
    claim_generation: int
    changed: bool
