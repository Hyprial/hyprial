"""Local X25519 records, not signed declaration or provider wire schemas."""
from __future__ import annotations

from dataclasses import dataclass, field


def signer_kind_allowed(key_kind: str, signer_kind: str) -> bool:
    """Policy prerequisite only; this does not authenticate a signer."""
    return (key_kind, signer_kind) in (("user", "owner"), ("host", "device"))


@dataclass(frozen=True)
class Record:
    kind: str
    casdoor_sub: str | None
    device_uid: str
    fingerprint: str
    key_id: str
    public_key: str
    private_key: str | None = field(repr=False)
    created_at: str
    state: str = "local"

    def public(self) -> dict:
        return {"algorithm": "X25519", "keyKind": self.kind,
                "casdoorSub": self.casdoor_sub, "deviceUid": self.device_uid,
                "keyId": self.key_id, "publicKey": self.public_key, "state": self.state}

    def disk(self) -> dict:
        value = dict(self.public(), schemaVersion=1, machineFingerprint=self.fingerprint,
                     createdAt=self.created_at)
        if self.private_key is not None:
            value["privateKey"] = self.private_key
        return value
