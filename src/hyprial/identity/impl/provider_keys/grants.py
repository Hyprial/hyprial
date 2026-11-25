"""R1 signed grants: owner signatures with exact envelope/agent/recipient binding."""
from __future__ import annotations

from uuid import UUID

from .models import (
    GRANT_PURPOSE, OwnerKey, SigningKey, integer, object_fields, require,
    sign, snapshot, text, validate_header, verify,
)


def _validate(payload: dict) -> None:
    object_fields(payload, {"v", "provider", "keyId", "owner", "agent", "dekVersion", "recipientKeyId", "issuedAt"},
                  {"expiresAt"})
    require(type(payload["v"]) is int and payload["v"] == 1)
    require(all(text(payload[name]) for name in ("provider", "keyId", "owner", "agent", "recipientKeyId")))
    require(integer(payload["dekVersion"], 1) and integer(payload["issuedAt"]))
    if "expiresAt" in payload:
        require(integer(payload["expiresAt"]) and payload["expiresAt"] > payload["issuedAt"])
    try:
        agent = str(UUID(payload["agent"]))
    except ValueError:
        agent = None
    require(agent is not None and agent == payload["agent"])
    kid = payload["recipientKeyId"]
    require(kid.startswith("x25519:") and len(kid) == 39 and all(c in "0123456789abcdef" for c in kid[7:]))


def sign_grant(payload: dict, *, signer: SigningKey) -> dict:
    payload = snapshot(payload)
    _validate(payload)
    return {"payload": payload, "sealedBy": sign(payload, signer, GRANT_PURPOSE, owner=payload["owner"])}


def verify_grant(grant: dict, *, owner_key: OwnerKey, header: dict, agent: str,
                 recipient_key_id: str, now: int) -> dict:
    """Verify the candidate, not its global freshness. now is explicit Unix ms."""
    value = snapshot(grant)
    object_fields(value, {"payload", "sealedBy"})
    payload = value["payload"]
    _validate(payload)
    verify(payload, value["sealedBy"], owner_key, GRANT_PURPOSE, owner=payload["owner"])
    header = snapshot(header)
    validate_header(header)
    require(all(payload[name] == header[name] for name in ("provider", "keyId", "owner", "dekVersion")))
    require(payload["agent"] == agent and payload["recipientKeyId"] == recipient_key_id)
    require(integer(now) and payload["issuedAt"] <= now)
    require("expiresAt" not in payload or now < payload["expiresAt"])
    return payload
