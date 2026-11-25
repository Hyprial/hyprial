"""Closed §3.2/§3.3 schemas: tamper, unknown/missing fields, bounds and state rules.

Bad payloads are re-signed by the independent builder so only the schema, not
the signature, can refuse them.
"""
import copy

import pytest

from hyprial.kernel import ipc_errors
from record_vectors import (
    AGENT, GRANT_PURPOSE, OWNER_PRIVATE, RECORD_PURPOSE, deleted_key_payload, grant_payload,
    grant_state_payload, key_payload, raw_sign, recipient_key_id,
)

INVALID = ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
LEGACY = {"entryId": "openai", "fieldName": "api-key"}
LEGACY_GRANT = {"entryId": "openai", "fieldName": "api-key", "revision": 1}


def recipient(**changes):
    row = {"recipientKeyId": recipient_key_id(), "role": "owner-device", "deviceUid": "device-a"}
    row.update(changes)
    return row


def signed_grant(**changes):
    return raw_sign(GRANT_PURPOSE, grant_payload(**changes))


def without(build, name):
    payload = build()
    payload.pop(name)
    return payload


def refused(core, owner_key, signer, payload):
    record_type = payload.get("type") if payload.get("type") in (
        "provider-key", "provider-grant-state") else "provider-key"
    with pytest.raises(core.Error) as raised:
        core.verify_record(raw_sign(RECORD_PURPOSE, payload), owner_key=owner_key, record_type=record_type)
    assert raised.value.code == INVALID
    with pytest.raises(core.Error) as raised:
        core.sign_record(payload, signer=signer)
    assert raised.value.code == INVALID


def accepted(core, owner_key, signer, payload):
    record = core.sign_record(payload, signer=signer)
    assert core.verify_record(record, owner_key=owner_key, record_type=payload["type"]) == payload


@pytest.mark.parametrize("build", [key_payload, deleted_key_payload, grant_state_payload])
def test_every_signed_field_tamper_is_refused(core, signer, owner_key, build):
    record = core.sign_record(build(), signer=signer)
    for name, value in record["payload"].items():
        for replacement in ({"x": 1}, None, 2, "tampered"):
            if replacement == value:
                continue
            tampered = copy.deepcopy(record)
            tampered["payload"][name] = replacement
            with pytest.raises(core.Error) as raised:
                core.verify_record(tampered, owner_key=owner_key, record_type=build()["type"])
            assert raised.value.code == INVALID, name
        missing = copy.deepcopy(record)
        missing["payload"].pop(name)
        with pytest.raises(core.Error):
            core.verify_record(missing, owner_key=owner_key, record_type=build()["type"])


@pytest.mark.parametrize("build", [key_payload, deleted_key_payload, grant_state_payload])
def test_unknown_and_missing_fields_refused_even_when_signed(core, signer, owner_key, build):
    refused(core, owner_key, signer, build(extra=1))
    for name in build():
        refused(core, owner_key, signer, without(build, name))


@pytest.mark.parametrize("build", [key_payload, grant_state_payload])
def test_valid_variants_are_accepted(core, signer, owner_key, build):
    accepted(core, owner_key, signer, build())


@pytest.mark.parametrize("payload", [
    key_payload(legacyRef=LEGACY),
    key_payload(label=""),
    key_payload(label="x" * 128),
    key_payload(label="日本語 ✓ key"),
    key_payload(provider="A" + "b" * 127),
    key_payload(provider="open.ai_x-1"),
    key_payload(environmentNames=[f"N{i}" for i in range(8)]),
    key_payload(updatedAt=5000, seq=9007199254740991),
    key_payload(recipients=[recipient(), recipient(recipientKeyId="x25519:" + "0" * 32, role="host",
                                                  deviceUid="device-b")]),
    key_payload(recipients=[recipient(recipientKeyId="x25519:" + f"{i:032x}") for i in range(32)]),
    deleted_key_payload(),
    deleted_key_payload(legacyRef=LEGACY),
], ids=lambda p: str(p.get("label"))[:12] + str(p.get("state")))
def test_key_bounds_accept(core, signer, owner_key, payload):
    accepted(core, owner_key, signer, payload)


@pytest.mark.parametrize("payload", [
    key_payload(type="provider-keys"),
    key_payload(v=2), key_payload(v=True), key_payload(v="1"),
    key_payload(owner=""), key_payload(owner="o" * 257), key_payload(owner="a\0b"), key_payload(owner=7),
    key_payload(keyId="pk_test"), key_payload(keyId="pk." + "a" * 25), key_payload(keyId="pk." + "a" * 27),
    key_payload(keyId="pk." + "A" * 26), key_payload(keyId="pk." + "1" * 26), key_payload(keyId="pk." + "8" * 26),
    key_payload(provider=""), key_payload(provider="-openai"), key_payload(provider=".x"),
    key_payload(provider="a" * 129), key_payload(provider="open/ai"), key_payload(provider="open ai"),
    key_payload(provider="openai\n"), key_payload(provider="ⓐ"),
    key_payload(kind="oauth"), key_payload(kind="API-KEY"), key_payload(kind=None),
    key_payload(label="x" * 129), key_payload(label="a\nb"), key_payload(label="a\tb"),
    key_payload(label="a​b"), key_payload(label="a\x7fb"), key_payload(label="a\u0085b"), key_payload(label=None),
    key_payload(state="revoked"), key_payload(state="ACTIVE"),
    key_payload(seq=0), key_payload(seq=-1), key_payload(seq=True), key_payload(seq="1"),
    key_payload(createdAt=-1), key_payload(createdAt=False), key_payload(updatedAt=999),
    key_payload(updatedAt="1000"),
    key_payload(dekVersion=0), key_payload(dekVersion=None), key_payload(dekVersion=True),
    key_payload(envelopeDigest=None), key_payload(envelopeDigest="A" * 64), key_payload(envelopeDigest="a" * 63),
    key_payload(envelopeDigest="g" * 64),
    key_payload(recipients=[]), key_payload(recipients=None), key_payload(recipients={}),
    key_payload(recipients=[recipient()] * 2),
    key_payload(recipients=[recipient(recipientKeyId="x25519:" + f"{i:032x}") for i in range(33)]),
    key_payload(recipients=[recipient(role="device")]), key_payload(recipients=[recipient(role=None)]),
    key_payload(recipients=[recipient(deviceUid="")]), key_payload(recipients=[recipient(deviceUid=7)]),
    key_payload(recipients=[recipient(recipientKeyId="ed25519:" + "0" * 32)]),
    key_payload(recipients=[recipient(recipientKeyId="x25519:" + "A" * 32)]),
    key_payload(recipients=[recipient(recipientKeyId="x25519:" + "0" * 31)]),
    key_payload(recipients=[recipient(extra=1)]), key_payload(recipients=[{"recipientKeyId": recipient_key_id()}]),
    key_payload(recipients=["x25519:" + "0" * 32]),
    key_payload(environmentNames=[]), key_payload(environmentNames=["A"] * 2),
    key_payload(environmentNames=[f"N{i}" for i in range(9)]), key_payload(environmentNames=["lower"]),
    key_payload(environmentNames=["1ABC"]), key_payload(environmentNames=["A-B"]), key_payload(environmentNames=["A\n"]),
    key_payload(environmentNames="A"), key_payload(environmentNames=[1]),
    key_payload(legacyRef={"entryId": "openai"}), key_payload(legacyRef={**LEGACY, "revision": 1}),
    key_payload(legacyRef={"entryId": "openai", "fieldName": "a/b"}),
    key_payload(legacyRef={"entryId": "", "fieldName": "x"}), key_payload(legacyRef="openai"),
    deleted_key_payload(dekVersion=1), deleted_key_payload(envelopeDigest="a" * 64),
    deleted_key_payload(recipients=[recipient()]),
])
def test_key_bounds_refuse(core, signer, owner_key, payload):
    refused(core, owner_key, signer, payload)


@pytest.mark.parametrize("payload", [
    grant_state_payload(legacyRef=LEGACY_GRANT),
    grant_state_payload(grantId="openai.api-key_2"),
    grant_state_payload(state="revoked", grant=None, seq=2),
    grant_state_payload(state="revoked", grant=None, keyId=None, seq=2),
    grant_state_payload(state="awaiting-key", grant=None, keyId=None, legacyRef=LEGACY_GRANT),
    grant_state_payload(state="awaiting-key", grant=None, keyId=None,
                        legacyRef={**LEGACY_GRANT, "revision": 9007199254740991}),
], ids=["legacy", "grantid", "revoked", "revoked-nokey", "awaiting", "awaiting-maxrev"])
def test_grant_state_bounds_accept(core, signer, owner_key, payload):
    accepted(core, owner_key, signer, payload)


@pytest.mark.parametrize("payload", [
    grant_state_payload(type="provider-grant"), grant_state_payload(v=2),
    grant_state_payload(agent=AGENT.upper()), grant_state_payload(agent=AGENT.replace("-", "")),
    grant_state_payload(agent="{" + AGENT + "}"), grant_state_payload(agent="urn:uuid:" + AGENT),
    grant_state_payload(agent="not-a-uuid"), grant_state_payload(agent=None),
    grant_state_payload(grantId=""), grant_state_payload(grantId="../x"), grant_state_payload(grantId="a" * 129),
    grant_state_payload(provider="open/ai"),
    grant_state_payload(state="deleted"), grant_state_payload(seq=0), grant_state_payload(updatedAt=-1),
    grant_state_payload(keyId="pk_test"),
    grant_state_payload(keyId=None),
    grant_state_payload(grant=None),
    grant_state_payload(state="revoked", seq=2),
    grant_state_payload(state="awaiting-key", grant=None, keyId=None),
    grant_state_payload(state="awaiting-key", grant=None, legacyRef=LEGACY_GRANT),
    grant_state_payload(state="awaiting-key", keyId=None, legacyRef=LEGACY_GRANT),
    grant_state_payload(legacyRef=LEGACY), grant_state_payload(legacyRef={**LEGACY_GRANT, "revision": 0}),
    grant_state_payload(legacyRef={**LEGACY_GRANT, "revision": True}),
    grant_state_payload(legacyRef={**LEGACY_GRANT, "extra": 1}),
    grant_state_payload(environmentNames=[]), grant_state_payload(environmentNames=["a"]),
    grant_state_payload(grant=signed_grant(provider="other")),
    grant_state_payload(grant=signed_grant(keyId="pk." + "b" * 26)),
    grant_state_payload(grant=signed_grant(owner="other-sub")),
    grant_state_payload(grant=signed_grant(agent="12345678-1234-4234-8234-123456789abd")),
    grant_state_payload(grant=raw_sign(GRANT_PURPOSE, {k: v for k, v in grant_payload().items() if k != "expiresAt"})),
    grant_state_payload(grant=signed_grant(expiresAt=1000)),
    grant_state_payload(grant=signed_grant(extra=1)),
    grant_state_payload(grant=signed_grant(recipientKeyId="x25519:short")),
    grant_state_payload(grant={"payload": grant_payload()}),
    grant_state_payload(grant={**signed_grant(), "extra": 1}),
    grant_state_payload(grant={"payload": grant_payload(), "sealedBy": {"keyId": 1, "sig": "x"}}),
    grant_state_payload(grant="signed"),
])
def test_grant_state_bounds_refuse(core, signer, owner_key, payload):
    refused(core, owner_key, signer, payload)


def test_owner_upper_bound_accepted_for_matching_anchor(core, owner_key):
    owner = "o" * 256
    signer = core.SigningKey(owner, OWNER_PRIVATE)
    anchor = core.OwnerKey(owner, owner_key.key_id, owner_key.public_key)
    state = grant_state_payload(owner=owner, grant=signed_grant(owner=owner))
    for payload in (key_payload(owner=owner), state):
        record = core.sign_record(payload, signer=signer)
        assert core.verify_record(record, owner_key=anchor, record_type=payload["type"]) == payload
