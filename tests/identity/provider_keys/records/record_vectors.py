"""Independent owner-record vectors. Every private value here is public test data.

The builder signs with raw cryptography primitives and U1 encoding, never the
product record functions, so the fixture pins the frozen signing input.
"""
import base64
import copy
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from hyprial.identity import UserKey

NOTICE = "PUBLIC SYNTHETIC TEST KEYS ONLY; never use these keys in production"
OWNER = "test-sub"
OWNER_PRIVATE = bytes(range(32))
RECIPIENT_PRIVATE = bytes(range(32, 64))
KEY_ID = "pk.mfrggzdfmztwq2lknnwg23tpob"
AGENT = "12345678-1234-4234-8234-123456789abc"
DAY_MS = 24 * 60 * 60 * 1000
RECORD_PURPOSE = b"hyprial/provider-key/record/v1"
GRANT_PURPOSE = b"hyprial/provider-key/grant/v1"
ENVELOPE_PURPOSE = b"hyprial/provider-key/envelope/v1"


def b64(value):
    return base64.b64encode(value).decode()


def kid(kind, public):
    return kind + ":" + hashlib.sha256(public).hexdigest()[:32]


def owner_public():
    return Ed25519PrivateKey.from_private_bytes(OWNER_PRIVATE).public_key().public_bytes_raw()


def recipient_public():
    return X25519PrivateKey.from_private_bytes(RECIPIENT_PRIVATE).public_key().public_bytes_raw()


def recipient_key_id():
    return kid("x25519", recipient_public())


def raw_sign(purpose, payload, private=OWNER_PRIVATE):
    signing_input = purpose + b"\0" + hashlib.sha256(UserKey.canonical_bytes(payload)).digest()
    key = Ed25519PrivateKey.from_private_bytes(private)
    return {"payload": copy.deepcopy(payload),
            "sealedBy": {"keyId": kid("ed25519", key.public_key().public_bytes_raw()),
                         "sig": b64(key.sign(signing_input))}}


def key_payload(**changes):
    payload = {
        "type": "provider-key", "v": 1, "owner": OWNER, "keyId": KEY_ID,
        "provider": "synthetic", "kind": "api-key", "label": "synthetic test key",
        "state": "active", "seq": 1, "createdAt": 1000, "updatedAt": 1000,
        "dekVersion": 1, "envelopeDigest": hashlib.sha256(b"synthetic-envelope").hexdigest(),
        "recipients": [{"recipientKeyId": recipient_key_id(), "role": "owner-device",
                        "deviceUid": "device-a"}],
        "environmentNames": ["SYNTHETIC_API_KEY"], "legacyRef": None,
    }
    payload.update(changes)
    return payload


def deleted_key_payload(**changes):
    values = {"state": "deleted", "seq": 2, "updatedAt": 2000, "dekVersion": None,
              "envelopeDigest": None, "recipients": []}
    values.update(changes)
    return key_payload(**values)


def grant_payload(**changes):
    payload = {"v": 1, "provider": "synthetic", "keyId": KEY_ID, "owner": OWNER, "agent": AGENT,
               "dekVersion": 1, "recipientKeyId": recipient_key_id(), "issuedAt": 1000,
               "expiresAt": 1000 + DAY_MS}
    payload.update(changes)
    return payload


def grant_state_payload(**changes):
    payload = {
        "type": "provider-grant-state", "v": 1, "owner": OWNER, "agent": AGENT,
        "grantId": "synthetic", "provider": "synthetic", "state": "active", "seq": 1,
        "keyId": KEY_ID, "grant": raw_sign(GRANT_PURPOSE, grant_payload()),
        "environmentNames": ["SYNTHETIC_API_KEY"], "legacyRef": None, "updatedAt": 1000,
    }
    payload.update(changes)
    return payload


def records_fixture():
    return {"notice": NOTICE, "ownerPrivate": b64(OWNER_PRIVATE), "ownerPublic": b64(owner_public()),
            "ownerKeyId": kid("ed25519", owner_public()), "owner": OWNER,
            "recordPurpose": RECORD_PURPOSE.decode(),
            "keyRecord": raw_sign(RECORD_PURPOSE, key_payload()),
            "grantStateRecord": raw_sign(RECORD_PURPOSE, grant_state_payload())}
