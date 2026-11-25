"""Ephemeral test issuer. Never contacts Casdoor or uses account credentials."""
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def issuer():
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    key_id = "ed25519:" + hashlib.sha256(public).hexdigest()[:32]
    binding = dict(iss="https://issuer.test.invalid", aud="hyprial-user-key",
                   sub="test-sub", name="alice@test.invalid", owner="hyprial",
                   keyId=key_id, publicKey=base64.b64encode(public).decode(),
                   keyAlg="Ed25519", deviceUid="test-device", iat=1000, nbf=1000,
                   exp=1000 + 30 * 86400, jti="test-jti")
    revocations = dict(iss=binding["iss"], aud="hyprial-user-key", sub="test-sub",
                       seq=1, revokedKeyIds=[], iat=1000, exp=1000 + 86400)

    def token(claims, typ="hyprial-user-key-binding+jwt", **header_changes):
        header = dict(alg="RS256", kid="test-rsa", typ=typ)
        header.update(header_changes)
        content = ".".join(b64(json.dumps(v, separators=(",", ":")).encode()) for v in (header, claims))
        signature = signing.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
        return content + "." + b64(signature)

    return SimpleNamespace(signing=signing, keys={"test-rsa": signing.public_key()},
                           binding=binding, revocations=revocations, token=token, now=1001)
