"""Ed25519 primitives and bounded deterministic JSON for local test envelopes.

No provider envelope or grant wire format is defined here. The integer-only
JSON subset avoids platform-dependent floats and rejects surrogate codepoints.
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from hyprial.kernel import ipc_errors
from .models import Error
from .purposes import Purpose


def canonical_bytes(value: object) -> bytes:
    def checked(item, depth=0):
        if depth > 20:
            raise ValueError
        if item is None or type(item) is bool:
            return item
        if type(item) is int and abs(item) <= 9007199254740991:
            return item
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
            return item
        if isinstance(item, list):
            return [checked(v, depth + 1) for v in item]
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            return {k: checked(item[k], depth + 1)
                    for k in sorted(item, key=lambda s: s.encode("utf-16-be"))}
        raise ValueError
    try:
        encoded = json.dumps(checked(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > 65536:
            raise ValueError
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Error(ipc_errors.PAYLOAD_INVALID) from None


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def decode(value: str, length: int) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != length or base64.b64encode(raw).decode() != value:
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise Error(ipc_errors.USER_KEY_QUARANTINED) from None


def key_id(public: bytes) -> str:
    return "ed25519:" + hashlib.sha256(public).hexdigest()[:32]


def generate() -> tuple[str, str, str]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    return (key_id(public), base64.b64encode(public).decode(),
            base64.b64encode(key.private_bytes_raw()).decode())


def signing_input(purpose: Purpose, payload: dict) -> bytes:
    if (purpose.domain not in {"hyprial-orgfs-decl-v1", "hyprial-user-op-v1"}
            or not purpose.kind or "\x00" in purpose.kind or purpose.key_kind != "owner"):
        raise Error(ipc_errors.PURPOSE_UNKNOWN)
    return purpose.domain.encode() + b"\0" + purpose.kind.encode() + b"\0" + canonical_bytes(payload)


def sign(private_key: str, purpose: Purpose, payload: dict) -> str:
    return base64.b64encode(Ed25519PrivateKey.from_private_bytes(decode(private_key, 32))
                            .sign(signing_input(purpose, payload))).decode()


def verify(envelope: dict, public_key: str, *, purpose: Purpose) -> bool:
    try:
        if set(envelope) != {"domain", "kind", "payload", "signer", "signature"}:
            return False
        signer = envelope["signer"]
        public = decode(public_key, 32)
        if (envelope["domain"] != purpose.domain or envelope["kind"] != purpose.kind
                or set(signer) != {"keyKind", "owner", "keyId"}
                or signer["keyKind"] != purpose.key_kind
                or signer["keyId"] != key_id(public)
                or signer["owner"] != envelope["payload"]["owner"]
                or purpose.fields is None or set(envelope["payload"]) != purpose.fields
                or type(envelope["payload"]["issuedAtMs"]) is not int):
            return False
        payload = dict(envelope["payload"])
        issued = payload.pop("issuedAtMs")
        normalized = purpose.normalize(payload, owner=signer["owner"], now_ms=issued)
        Ed25519PublicKey.from_public_bytes(public).verify(
            decode(envelope["signature"], 64), signing_input(purpose, normalized))
        return True
    except (Error, InvalidSignature, ValueError, TypeError, KeyError, AttributeError):
        return False
