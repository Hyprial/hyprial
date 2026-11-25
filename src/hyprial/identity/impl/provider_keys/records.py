# Decision record (provider-key contract freeze r3 §2, §3.1-§3.3, HQ F-2):
# - One purpose, hyprial/provider-key/record/v1, signs every owner-held typed
#   record; it differs from the envelope and grant purposes, so no signature can
#   be replayed across them.
# - verify_record checks the owner signature over a detached snapshot BEFORE any
#   field is trusted, then applies the closed schema; every refusal is the same
#   value-free PROVIDER_KEY_SIGNATURE_INVALID.
"""Owner-signed provider-key metadata and grant-state records (pure, no I/O)."""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import unicodedata
import uuid

from .grants import _validate as validate_grant_payload
from .models import (
    INVALID, MAX_RECIPIENTS, RECORD_PURPOSE, Error, OwnerKey, SigningKey, encode, integer,
    object_fields, require, sign, snapshot, text, verify,
)

KEY_TYPE = "provider-key"
GRANT_STATE_TYPE = "provider-grant-state"
RECORD_TYPES = (KEY_TYPE, GRANT_STATE_TYPE)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_KEY_ID = re.compile(r"pk\.[a-z2-7]{26}")
_ENV = re.compile(r"[A-Z][A-Z0-9_]*")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_RECIPIENT = re.compile(r"x25519:[0-9a-f]{32}")
_KEY_FIELDS = {
    "type", "v", "owner", "keyId", "provider", "kind", "label", "state", "seq", "createdAt",
    "updatedAt", "dekVersion", "envelopeDigest", "recipients", "environmentNames", "legacyRef",
}
_GRANT_STATE_FIELDS = {
    "type", "v", "owner", "agent", "grantId", "provider", "state", "seq", "keyId", "grant",
    "environmentNames", "legacyRef", "updatedAt",
}


def new_key_id() -> str:
    """Allocate pk. plus 26 lowercase unpadded base32 characters from 128 CSPRNG bits."""
    return "pk." + base64.b32encode(secrets.token_bytes(16)).decode("ascii").lower().rstrip("=")


def agent_uuid(entity_token: str) -> str:
    """Map a 32-character lowercase hex entity token to its canonical UUID."""
    require(type(entity_token) is str and len(entity_token) == 32
            and all(c in "0123456789abcdef" for c in entity_token))
    return str(uuid.UUID(hex=entity_token))


def record_digest(value: object) -> str:
    """SHA-256 hex of the canonical encoding (same rule as the E1 grant digest)."""
    return hashlib.sha256(encode(value)).hexdigest()


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _label(value: object) -> bool:
    return (type(value) is str and len(value) <= 128
            and not any(unicodedata.category(char).startswith("C") for char in value))


def _environment_names(value: object) -> bool:
    return (type(value) is list and 1 <= len(value) <= 8
            and all(_matches(_ENV, name) for name in value) and len(set(value)) == len(value))


def _legacy_ref(value: object, *, revision: bool) -> None:
    if value is None:
        return
    object_fields(value, {"entryId", "fieldName", "revision"} if revision else {"entryId", "fieldName"})
    require(_matches(_NAME, value["entryId"]) and _matches(_NAME, value["fieldName"]))
    require(not revision or integer(value["revision"], 1))


def _recipients(value: object) -> None:
    require(type(value) is list and 1 <= len(value) <= MAX_RECIPIENTS)
    seen = set()
    for row in value:
        object_fields(row, {"recipientKeyId", "role", "deviceUid"})
        require(_matches(_RECIPIENT, row["recipientKeyId"]) and row["recipientKeyId"] not in seen)
        require(row["role"] in ("owner-device", "host") and text(row["deviceUid"]))
        seen.add(row["recipientKeyId"])


def _common(payload: dict) -> None:
    require(type(payload["v"]) is int and payload["v"] == 1)
    require(text(payload["owner"]) and _matches(_NAME, payload["provider"]))
    require(integer(payload["seq"], 1) and integer(payload["updatedAt"]))
    require(_environment_names(payload["environmentNames"]))


def _validate_key(payload: dict) -> None:
    object_fields(payload, _KEY_FIELDS)
    _common(payload)
    require(_matches(_KEY_ID, payload["keyId"]) and payload["kind"] == "api-key")
    require(_label(payload["label"]) and payload["state"] in ("active", "deleted"))
    require(integer(payload["createdAt"]) and payload["createdAt"] <= payload["updatedAt"])
    if payload["state"] == "active":
        require(integer(payload["dekVersion"], 1) and _matches(_DIGEST, payload["envelopeDigest"]))
        _recipients(payload["recipients"])
    else:
        require(payload["dekVersion"] is None and payload["envelopeDigest"] is None)
        require(payload["recipients"] == [])
    _legacy_ref(payload["legacyRef"], revision=False)


def _validate_grant(grant: object, outer: dict) -> None:
    object_fields(grant, {"payload", "sealedBy"})
    object_fields(grant["sealedBy"], {"keyId", "sig"})
    require(text(grant["sealedBy"]["keyId"]) and text(grant["sealedBy"]["sig"], 4096))
    inner = grant["payload"]
    validate_grant_payload(inner)
    require(all(inner[name] == outer[name] for name in ("provider", "keyId", "owner", "agent")))
    require("expiresAt" in inner)


def _validate_grant_state(payload: dict) -> None:
    object_fields(payload, _GRANT_STATE_FIELDS)
    _common(payload)
    require(_canonical_uuid(payload["agent"]) and _matches(_NAME, payload["grantId"]))
    require(payload["keyId"] is None or _matches(_KEY_ID, payload["keyId"]))
    _legacy_ref(payload["legacyRef"], revision=True)
    state = payload["state"]
    if state == "active":
        require(payload["keyId"] is not None)
        _validate_grant(payload["grant"], payload)
    elif state == "revoked":
        require(payload["grant"] is None)
    else:
        require(state == "awaiting-key" and payload["keyId"] is None and payload["grant"] is None)
        require(payload["legacyRef"] is not None)


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _validate(payload: object) -> None:
    require(type(payload) is dict and payload.get("type") in RECORD_TYPES)
    if payload["type"] == KEY_TYPE:
        _validate_key(payload)
    else:
        _validate_grant_state(payload)


def sign_record(payload: dict, *, signer: SigningKey) -> dict:
    """Validate a detached payload strictly, then sign it with the record purpose."""
    payload = snapshot(payload)
    _validate(payload)
    return {"payload": payload, "sealedBy": sign(payload, signer, RECORD_PURPOSE, owner=payload["owner"])}


def verify_record(record: dict, *, owner_key: OwnerKey, record_type: str) -> dict:
    """Verify the owner signature first, then the closed schema; return the detached payload."""
    try:
        require(record_type in RECORD_TYPES and isinstance(owner_key, OwnerKey))
        value = snapshot(record)
        object_fields(value, {"payload", "sealedBy"})
        payload = value["payload"]
        verify(payload, value["sealedBy"], owner_key, RECORD_PURPOSE, owner=owner_key.owner)
        _validate(payload)
        require(payload["type"] == record_type and payload["owner"] == owner_key.owner)
        return payload
    except Error:
        raise Error(INVALID) from None




_IDENTITY_FIELDS = {
    KEY_TYPE: ("owner", "keyId", "provider", "kind", "createdAt", "legacyRef"),
    GRANT_STATE_TYPE: ("owner", "agent", "grantId"),
}


def check_transition(previous: dict | None, current: dict) -> None:
    """Refuse rollback, identity changes, edits after a key tombstone and DEK rollback (§4.3-§4.5).

    Between two active key records dekVersion never decreases, and an unchanged dekVersion
    keeps its envelopeDigest because envelope-<dekVersion>.json is create_only.
    """
    try:
        current = snapshot(current)
        _validate(current)
        if previous is None:
            return
        previous = snapshot(previous)
        _validate(previous)
        record_type = current["type"]
        require(previous["type"] == record_type)
        require(all(previous[name] == current[name] for name in _IDENTITY_FIELDS[record_type]))
        require(record_type != KEY_TYPE or previous["state"] != "deleted")
        require(current["seq"] > previous["seq"] and current["updatedAt"] >= previous["updatedAt"])
        if record_type == KEY_TYPE and previous["state"] == current["state"] == "active":
            require(current["dekVersion"] >= previous["dekVersion"])
            require(current["dekVersion"] != previous["dekVersion"]
                    or current["envelopeDigest"] == previous["envelopeDigest"])
    except Error:
        raise Error(INVALID) from None


def check_envelope_matches(meta_payload: dict, envelope: dict) -> None:
    """Bind an envelope to an active signed meta: digest, header identity and recipient set."""
    try:
        meta = snapshot(meta_payload)
        _validate(meta)
        require(meta["type"] == KEY_TYPE and meta["state"] == "active")
        value = snapshot(envelope)
        require(record_digest(value) == meta["envelopeDigest"])
        header, rows = value.get("header"), value.get("recipients")
        require(type(header) is dict and type(rows) is list and all(type(row) is dict for row in rows))
        require(all(type(header.get(name)) is type(meta[name]) and header[name] == meta[name]
                    for name in ("keyId", "owner", "provider", "dekVersion")))
        ids = [row.get("recipientKeyId") for row in rows]
        expected = {row["recipientKeyId"] for row in meta["recipients"]}
        require(len(ids) == len(expected) and set(ids) == expected)
    except Error:
        raise Error(INVALID) from None
