"""Strict, network-free RS256 user-key binding and revocation verification.

The caller supplies issuer-approved RSA keys. JWT headers never select a remote
key source. Self-published Ed25519 keys or a local bound flag are not anchors.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from hyprial.kernel import USER_KEY_REVOCATION_MAX_AGE, ipc_errors
from .crypto import decode, key_id
from .models import Error, KeyBinding, Record, Revocations, Trust

BINDING_TYP = "hyprial-user-key-binding+jwt"
REVOCATION_TYP = "hyprial-user-key-revocations+jwt"
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{32}\Z")


def unavailable_binding(record: Record) -> Trust:
    """Production remains unavailable until an issuer/source is explicitly wired."""
    raise Error(ipc_errors.USER_KEY_UNBOUND)


def valid_key_id(value: object) -> bool:
    return isinstance(value, str) and _KEY_ID.fullmatch(value) is not None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _segment(value: str) -> bytes:
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise ValueError
    return decoded


def _text(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _date(value) -> int:
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError
    return value


def _verified(token, *, typ, issuer, keys, now):
    if (not isinstance(token, str) or len(token) > 65536 or not _text(issuer)
            or type(now) not in (int, float) or not 0 <= now < 2**53 or not math.isfinite(now)):
        raise ValueError
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError
    header = json.loads(_segment(parts[0]), object_pairs_hook=_unique)
    claims = json.loads(_segment(parts[1]), object_pairs_hook=_unique)
    if (not isinstance(header, dict) or set(header) != {"alg", "kid", "typ"}
            or header["alg"] != "RS256" or header["typ"] != typ
            or not _text(header["kid"]) or not isinstance(claims, dict)
            or not isinstance(keys, Mapping)):
        raise ValueError
    key = keys.get(header["kid"])
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ValueError
    key.verify(_segment(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"),
               padding.PKCS1v15(), hashes.SHA256())
    if claims.get("iss") != issuer or claims.get("aud") != "hyprial-user-key":
        raise ValueError
    return header, claims


def verify_binding(token: str, *, issuer: str, keys: Mapping, now: float) -> KeyBinding:
    """Authenticate a binding, not its correspondence to a requested owner."""
    try:
        header, claims = _verified(token, typ=BINDING_TYP, issuer=issuer, keys=keys, now=now)
        fields = {"iss", "aud", "sub", "name", "owner", "keyId", "publicKey", "keyAlg",
                  "deviceUid", "iat", "nbf", "exp", "jti"}
        if (set(claims) != fields or claims["owner"] != "hyprial" or claims["keyAlg"] != "Ed25519"
                or any(not _text(claims[k]) for k in fields - {"iat", "nbf", "exp"})):
            raise ValueError
        issued, not_before, expires = (_date(claims[k]) for k in ("iat", "nbf", "exp"))
        if not issued <= not_before <= now < expires <= issued + 30 * 86400:
            raise ValueError
        public = decode(claims["publicKey"], 32)
        if claims["keyId"] != key_id(public):
            raise ValueError
        return KeyBinding(claims["name"], claims["sub"], claims["deviceUid"],
                          claims["keyId"], claims["publicKey"], expires, header["kid"],
                          hashlib.sha256(token.encode("ascii")).hexdigest())
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, InvalidSignature, Error):
        raise Error(ipc_errors.USER_KEY_UNBOUND) from None


def verify_revocations(token: str, *, issuer: str, keys: Mapping, now: float) -> Revocations:
    """Authenticate a fresh statement; sequence rollback is the resolver's job."""
    try:
        _, claims = _verified(token, typ=REVOCATION_TYP, issuer=issuer, keys=keys, now=now)
        if (set(claims) != {"iss", "aud", "sub", "seq", "revokedKeyIds", "iat", "exp"}
                or not _text(claims["sub"])):
            raise ValueError
        issued, expires, seq = (_date(claims[k]) for k in ("iat", "exp", "seq"))
        if not issued <= now < expires <= issued + USER_KEY_REVOCATION_MAX_AGE:
            raise ValueError
        revoked = claims["revokedKeyIds"]
        if (not isinstance(revoked, list) or len(revoked) > 4096
                or any(not valid_key_id(k) for k in revoked) or len(set(revoked)) != len(revoked)):
            raise ValueError
        return Revocations(claims["sub"], seq, frozenset(revoked), issued, expires)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, InvalidSignature, Error):
        raise Error(ipc_errors.USER_KEY_UNBOUND) from None
