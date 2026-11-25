"""Independent R1 test-vector construction. All private values are public test data."""
import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hyprial.identity import UserKey


def b64(value):
    return base64.b64encode(value).decode()


def kid(kind, public):
    return kind + ":" + hashlib.sha256(public).hexdigest()[:32]


def envelope_vector():
    owner_private = bytes(range(32))
    owner = Ed25519PrivateKey.from_private_bytes(owner_private)
    public = owner.public_key().public_bytes_raw()
    recipients = []
    for start in (32, 64):
        private = bytes(range(start, start + 32))
        pub = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
        recipients.append({"private": b64(private), "public": b64(pub), "keyId": kid("x25519", pub)})
    header = {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM",
              "provider": "synthetic", "keyId": "pk_test", "owner": "test-sub",
              "dekVersion": 1, "createdAt": 1000}
    aad = UserKey.canonical_bytes(header)
    secret = b"SYNTHETIC-PROVIDER-SECRET-NEVER-VALID"
    random_values = [bytes(range(96, 128)), bytes(range(12))]
    payload = {"nonce": b64(random_values[1]),
               "ciphertext": b64(AESGCM(random_values[0]).encrypt(random_values[1], secret, aad))}
    wrapped = []
    wrapping_keys = []
    for index, recipient in enumerate(recipients):
        ephemeral_private = bytes(range(128 + 32 * index, 160 + 32 * index))
        nonce = bytes(range(12 + 12 * index, 24 + 12 * index))
        random_values.extend([ephemeral_private, nonce])
        ephemeral = X25519PrivateKey.from_private_bytes(ephemeral_private)
        epk = ephemeral.public_key().public_bytes_raw()
        pub = base64.b64decode(recipient["public"])
        peer = X25519PrivateKey.from_private_bytes(base64.b64decode(recipient["private"])).public_key()
        info = b"hyprial/provider-key/wrap/v1\0" + aad + b"\0" + recipient["keyId"].encode()
        kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + pub, info=info).derive(ephemeral.exchange(peer))
        wrapping_keys.append(b64(kek))
        wrapped.append({"recipientKeyId": recipient["keyId"], "epk": b64(epk), "nonce": b64(nonce),
                        "wrappedDek": b64(AESGCM(kek).encrypt(nonce, random_values[0], aad + b"\0" + recipient["keyId"].encode()))})
    unsigned = {"header": header, "recipients": wrapped, "payload": payload}
    signing_input = b"hyprial/provider-key/envelope/v1\0" + hashlib.sha256(UserKey.canonical_bytes(unsigned)).digest()
    envelope = {**unsigned, "sealedBy": {"keyId": kid("ed25519", public), "sig": b64(owner.sign(signing_input))}}
    return {"notice": "PUBLIC SYNTHETIC TEST KEYS ONLY; never use these keys or nonces in production",
            "ownerPrivate": b64(owner_private), "ownerPublic": b64(public), "ownerKeyId": kid("ed25519", public),
            "recipients": recipients, "secret": b64(secret), "randomValues": [b64(v) for v in random_values],
            "headerAad": b64(aad), "wrappingKeys": wrapping_keys,
            "signingInput": b64(signing_input), "envelope": envelope}


def grant_vector():
    fixture = envelope_vector()
    payload = {"v": 1, "provider": "synthetic", "keyId": "pk_test", "owner": "test-sub",
               "agent": "12345678-1234-4234-8234-123456789abc", "dekVersion": 1,
               "recipientKeyId": fixture["recipients"][0]["keyId"], "issuedAt": 1000, "expiresAt": 3000}
    raw = b"hyprial/provider-key/grant/v1\0" + hashlib.sha256(UserKey.canonical_bytes(payload)).digest()
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(fixture["ownerPrivate"]))
    return {"notice": fixture["notice"], "signingInput": b64(raw),
            "grant": {"payload": payload, "sealedBy": {"keyId": fixture["ownerKeyId"], "sig": b64(key.sign(raw))}}}
