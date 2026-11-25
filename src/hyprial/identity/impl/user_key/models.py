"""Local user-key types. Secret fields are never included in repr or status."""
from __future__ import annotations

from dataclasses import dataclass, field

from hyprial.kernel import DaemonRequestError, ipc_errors


class Error(DaemonRequestError):
    """Sanitized boundary error: never interpolate a record or a parser error."""

    def __init__(self, code: str):
        messages = {
            ipc_errors.USER_KEY_ABSENT: "user key absent; log in on this device",
            ipc_errors.USER_KEY_UNBOUND: "user key signing unavailable; trusted binding required",
            ipc_errors.USER_KEY_BINDING_EXPIRED: "user key binding expired; renew binding",
            ipc_errors.USER_KEY_QUARANTINED: "user key quarantined or storage unsafe; inspect local state",
            ipc_errors.PURPOSE_UNKNOWN: "user key purpose is not registered",
            ipc_errors.PAYLOAD_INVALID: "user key payload does not match its purpose schema",
            ipc_errors.RATE_LIMITED: "user key signing rate limit exceeded",
            ipc_errors.CALLER_NOT_AUTHORIZED: "caller is not authorized for user key operation",
        }
        super().__init__(code, messages.get(code, "user key operation refused"))


@dataclass(frozen=True)
class Record:
    owner: str
    casdoor_sub: str
    device_uid: str
    machine_fingerprint: str
    key_id: str
    public_key: str
    private_key: str | None = field(repr=False)
    created_at: str
    state: str = "unbound"

    def public(self) -> dict:
        return {"keyId": self.key_id, "publicKey": self.public_key,
                "state": self.state, "bindingExp": None, "revocationSeq": None}

    def disk(self, *, include_private: bool = True) -> dict:
        value = {"schemaVersion": 1, "algorithm": "Ed25519", "keyId": self.key_id,
                 "publicKey": self.public_key, "owner": self.owner,
                 "casdoorSub": self.casdoor_sub, "deviceUid": self.device_uid,
                 "machineFingerprint": self.machine_fingerprint,
                 "createdAt": self.created_at, "state": self.state}
        if include_private and self.private_key is not None:
            value["privateKey"] = self.private_key
        return value


@dataclass(frozen=True)
class KeyBinding:
    owner: str
    sub: str
    device_uid: str
    key_id: str
    public_key: str
    expires_at: int
    jwks_kid: str
    credential_sha256: str


@dataclass(frozen=True)
class Revocations:
    sub: str
    seq: int
    revoked_key_ids: frozenset[str]
    issued_at: int
    expires_at: int


@dataclass(frozen=True)
class Trust:
    """U2 supplies this only after verifying binding AND fresh revocations.

    expires_at is the minimum binding/revocation freshness deadline, in seconds.
    Local state='bound' alone is never evidence of trust.
    """
    public_key: str
    expires_at: float
    revocation_seq: int
