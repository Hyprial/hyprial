"""Shared issuer-exact, same-origin RS256 JWKS parsing and bounded cache.

HTTP transport is supplied by the owner. This module does not choose an issuer,
follow redirects, read account credentials or install a production key source.
"""
from __future__ import annotations

import base64
import threading
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa

from hyprial.kernel import IDENTITY_BINDING_JWKS_TTL_SECONDS


class JwksUnavailable(RuntimeError):
    """Sanitized failure of an issuer's discovery or public-key source."""


def _origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _integer(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        raise ValueError
    return int.from_bytes(raw, "big")


def load_keys(issuer, request_json):
    """Fetch and check discovery + JWKS; never use a token's jku/x5u."""
    try:
        origin = _origin(issuer)
        discovery = request_json(f"{issuer}/.well-known/openid-configuration")
        if not isinstance(discovery, dict) or discovery.get("issuer") != issuer:
            raise ValueError
        uri = discovery.get("jwks_uri")
        if not isinstance(uri, str) or _origin(uri) != origin:
            raise ValueError
        document = request_json(uri)
        values = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(values, list) or len(values) > 256:
            raise ValueError
        keys = {}
        for value in values:
            if (not isinstance(value, dict) or value.get("kty") != "RSA"
                    or value.get("alg") not in (None, "RS256")
                    or value.get("use") not in (None, "sig")
                    or ("key_ops" in value and value["key_ops"] != ["verify"])):
                continue
            kid = value.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > 1024:
                continue
            if kid in keys:
                raise ValueError
            key = rsa.RSAPublicNumbers(_integer(value.get("e")), _integer(value.get("n"))).public_key()
            if key.key_size < 2048:
                raise ValueError
            keys[kid] = key
        if not keys:
            raise ValueError
        return keys
    except Exception:
        raise JwksUnavailable("issuer public-key verification source unavailable") from None


class JwksSource:
    """An explicit source, with no stale-key fallback after the TTL expires."""
    def __init__(self, *, issuer, request_json, monotonic=time.monotonic):
        self.issuer, self.request_json = issuer, request_json
        self.monotonic = monotonic
        self._keys, self._loaded_at = None, None
        self._lock = threading.RLock()

    def __call__(self, *, refresh=False):
        with self._lock:
            now = self.monotonic()
            if (self._keys is None or self._loaded_at is None or refresh
                    or not 0 <= now - self._loaded_at < IDENTITY_BINDING_JWKS_TTL_SECONDS):
                self._keys = load_keys(self.issuer, self.request_json)
                self._loaded_at = now
            return dict(self._keys)
