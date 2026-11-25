"""Signed adapter authorization values; no SQL store or issuer is installed."""
from __future__ import annotations
import base64
import dataclasses
import hashlib
import json
from dataclasses import asdict, dataclass
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


@dataclass(frozen=True, slots=True)
class ServiceAuthorizationRefV1:
    lease_id: str
    lease_generation: int
    operation_id: str
    use_nonce: str
    lease_digest: str

def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")



@dataclass(frozen=True, slots=True)
class OrganizationAuthorizationLeaseV1:
    lease_id: str
    organization_id: str
    actor: str
    entity_token: str
    association_generation: int
    subject_principal: str
    scope: tuple[str, ...]
    membership_revision: str
    policy_revision: str
    evidence_digest: str
    generation: int
    issued_at_ms: int
    expires_at_ms: int
    nonce: str
    signer_public_key_b64: str
    signature_b64: str

    def signed_body(self) -> bytes:
        value = asdict(self)
        value.pop("signature_b64")
        return canonical_bytes(value)

    @property
    def lease_digest(self) -> str:
        return hashlib.sha256(
            b"hyprial-org-authorization-lease-v1\0" + self.signed_body()
        ).hexdigest()

    def to_bytes(self) -> bytes:
        return canonical_bytes(asdict(self))

    @classmethod
    def from_bytes(cls, payload: bytes) -> OrganizationAuthorizationLeaseV1:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("AUTHORIZATION_LEASE_INVALID") from error
        if not isinstance(value, dict) or set(value) != {
            field.name for field in dataclasses.fields(cls)
        }:
            raise ValueError("AUTHORIZATION_LEASE_INVALID")
        scope = value.get("scope")
        if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
            raise ValueError("AUTHORIZATION_LEASE_INVALID")
        value["scope"] = tuple(scope)
        try:
            return cls(**value)
        except TypeError as error:
            raise ValueError("AUTHORIZATION_LEASE_INVALID") from error

    def ref(self, *, operation_id: str, use_nonce: str) -> ServiceAuthorizationRefV1:
        return ServiceAuthorizationRefV1(
            lease_id=self.lease_id,
            lease_generation=self.generation,
            operation_id=operation_id,
            use_nonce=use_nonce,
            lease_digest=self.lease_digest,
        )

    def verify(self, *, now_ms: int, required_scope: tuple[str, ...]) -> None:
        if now_ms < self.issued_at_ms - 30_000 or now_ms >= self.expires_at_ms:
            raise ValueError("AUTHORIZATION_LEASE_EXPIRED")
        if self.expires_at_ms - self.issued_at_ms > 300_000:
            raise ValueError("AUTHORIZATION_LEASE_INVALID")
        if not set(required_scope).issubset(self.scope):
            raise ValueError("AUTHORIZATION_SCOPE_NOT_COVERED")
        public = base64.b64decode(self.signer_public_key_b64, validate=True)
        signature = base64.b64decode(self.signature_b64, validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature, b"hyprial-org-authorization-lease-v1\0" + self.signed_body()
        )
