"""Owner-record purpose, signing input, round trips, purpose separation and ids."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from hyprial.identity.impl.provider_keys import models
from hyprial.kernel import ipc_errors
from record_vectors import (
    AGENT, ENVELOPE_PURPOSE, GRANT_PURPOSE, OWNER, RECIPIENT_PRIVATE, RECORD_PURPOSE, grant_payload,
    grant_state_payload, key_payload, raw_sign, recipient_key_id, recipient_public, records_fixture,
)

INVALID = ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
FIXTURE = Path(__file__).resolve().parents[4] / "contract/fixtures/provider-keys/records.json"


def test_record_purpose_is_whitelisted_and_distinct():
    assert models.RECORD_PURPOSE == RECORD_PURPOSE
    value = {"type": "provider-key"}
    inputs = {purpose: models.signed_input(purpose, value)
              for purpose in (RECORD_PURPOSE, GRANT_PURPOSE, ENVELOPE_PURPOSE)}
    assert len(set(inputs.values())) == 3
    assert inputs[RECORD_PURPOSE] == RECORD_PURPOSE + b"\0" + hashlib.sha256(
        models.encode(value)).digest()
    for other in (b"hyprial/provider-key/record/v2", b"", RECORD_PURPOSE + b"\0"):
        with pytest.raises(models.Error) as raised:
            models.signed_input(other, value)
        assert raised.value.code == INVALID


@pytest.mark.parametrize("code", [ipc_errors.PROVIDER_STORE_UNAVAILABLE,
                                  ipc_errors.PROVIDER_STORE_CONFLICT])
def test_store_codes_have_fixed_specific_messages(code):
    error = models.Error(code)
    assert error.code == code
    assert str(error) not in ("provider key operation refused", "")
    assert str(models.Error(code)) == str(error)
    assert str(models.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)) != str(
        models.Error(ipc_errors.PROVIDER_STORE_CONFLICT))


@pytest.mark.parametrize("build", [key_payload, grant_state_payload])
def test_sign_verify_round_trip_both_types(core, signer, owner_key, build):
    payload = build()
    record = core.sign_record(payload, signer=signer)
    assert set(record) == {"payload", "sealedBy"}
    assert record == raw_sign(RECORD_PURPOSE, payload)
    verified = core.verify_record(copy.deepcopy(record), owner_key=owner_key,
                                  record_type=payload["type"])
    assert verified == payload
    verified["seq"] = 99
    assert record["payload"]["seq"] == 1


def test_sign_record_detaches_input(core, signer):
    payload = key_payload()
    record = core.sign_record(payload, signer=signer)
    payload["label"] = "changed"
    assert record["payload"]["label"] == "synthetic test key"


@pytest.mark.parametrize("build", [key_payload, grant_state_payload])
def test_record_type_must_match_expected(core, signer, owner_key, build):
    record = core.sign_record(build(), signer=signer)
    other = "provider-grant-state" if build is key_payload else "provider-key"
    for expected in (other, "provider-other", None):
        with pytest.raises(core.Error) as raised:
            core.verify_record(record, owner_key=owner_key, record_type=expected)
        assert raised.value.code == INVALID


@pytest.mark.parametrize("purpose", [GRANT_PURPOSE, ENVELOPE_PURPOSE])
def test_purpose_swap_is_refused_both_ways(core, signer, owner_key, purpose):
    swapped = raw_sign(purpose, key_payload())
    with pytest.raises(core.Error) as raised:
        core.verify_record(swapped, owner_key=owner_key, record_type="provider-key")
    assert raised.value.code == INVALID
    record = core.sign_record(grant_state_payload(), signer=signer)
    inner = record["payload"]["grant"]
    as_grant = {"payload": grant_payload(), "sealedBy": raw_sign(RECORD_PURPOSE, grant_payload())["sealedBy"]}
    header = {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM", "provider": "synthetic",
              "keyId": inner["payload"]["keyId"], "owner": OWNER, "dekVersion": 1, "createdAt": 1000}
    with pytest.raises(core.Error) as raised:
        core.verify_grant(as_grant, owner_key=owner_key, header=header, agent=AGENT,
                          recipient_key_id=inner["payload"]["recipientKeyId"], now=2000)
    assert raised.value.code == INVALID
    core.verify_grant(inner, owner_key=owner_key, header=header, agent=AGENT,
                      recipient_key_id=inner["payload"]["recipientKeyId"], now=2000)


def test_record_signature_is_refused_as_envelope_seal(core, signer, owner_key):
    header = {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM", "provider": "synthetic",
              "keyId": key_payload()["keyId"], "owner": OWNER, "dekVersion": 1, "createdAt": 1000}
    recipient = core.Recipient(recipient_key_id(), recipient_public())
    envelope = core.seal(b"SYNTHETIC-SECRET", header=header, recipients=[recipient], signer=signer)
    unsigned = {name: envelope[name] for name in ("header", "recipients", "payload")}
    opener = {"owner_key": owner_key, "recipient_key_id": recipient.key_id, "private_key": RECIPIENT_PRIVATE}
    control = {**unsigned, "sealedBy": raw_sign(ENVELOPE_PURPOSE, unsigned)["sealedBy"]}
    core.verify_envelope(control, owner_key=owner_key)
    assert core.open_envelope(control, **opener) == b"SYNTHETIC-SECRET"
    swapped = {**unsigned, "sealedBy": raw_sign(RECORD_PURPOSE, unsigned)["sealedBy"]}
    with pytest.raises(core.Error) as raised:
        core.verify_envelope(swapped, owner_key=owner_key)
    assert raised.value.code == INVALID
    with pytest.raises(core.Error) as raised:
        core.open_envelope(swapped, **opener)
    assert raised.value.code == INVALID


def test_wrong_owner_key_and_owner_mismatch_refused(core, signer, owner_key):
    record = core.sign_record(key_payload(), signer=signer)
    other_private = bytes(range(100, 132))
    other = raw_sign(RECORD_PURPOSE, key_payload(), private=other_private)
    with pytest.raises(core.Error):
        core.verify_record(other, owner_key=owner_key, record_type="provider-key")
    for anchor in (
        core.OwnerKey("other-sub", owner_key.key_id, owner_key.public_key),
        core.OwnerKey(OWNER, owner_key.key_id, owner_key.public_key, key_kind="device"),
        core.OwnerKey(OWNER, "ed25519:" + "0" * 32, owner_key.public_key),
        None,
    ):
        with pytest.raises(core.Error) as raised:
            core.verify_record(record, owner_key=anchor, record_type="provider-key")
        assert raised.value.code == INVALID
    with pytest.raises(core.Error):
        core.sign_record(key_payload(owner="other-sub"), signer=signer)
    forged = raw_sign(RECORD_PURPOSE, key_payload(owner="other-sub"))
    with pytest.raises(core.Error) as raised:
        core.verify_record(forged, owner_key=owner_key, record_type="provider-key")
    assert raised.value.code == INVALID


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(extra=1),
    lambda r: r.pop("sealedBy"),
    lambda r: r.pop("payload"),
    lambda r: r["sealedBy"].update(extra="x"),
    lambda r: r["sealedBy"].update(sig=base64.b64encode(bytes(64)).decode()),
    lambda r: r["sealedBy"].update(sig="not base64"),
    lambda r: r.update(payload=[1, 2]),
])
def test_top_level_shape_is_exact(core, signer, owner_key, mutate):
    record = core.sign_record(key_payload(), signer=signer)
    mutate(record)
    with pytest.raises(core.Error) as raised:
        core.verify_record(record, owner_key=owner_key, record_type="provider-key")
    assert raised.value.code == INVALID


@pytest.mark.parametrize("value", [None, [], "record", 7])
def test_non_object_record_refused(core, owner_key, value):
    with pytest.raises(core.Error) as raised:
        core.verify_record(value, owner_key=owner_key, record_type="provider-key")
    assert raised.value.code == INVALID


def test_errors_never_echo_input(core, owner_key):
    secretish = "SYNTHETIC-NEVER-ECHO-" + "x" * 20
    record = raw_sign(RECORD_PURPOSE, key_payload(label=secretish, extra=secretish))
    with pytest.raises(core.Error) as raised:
        core.verify_record(record, owner_key=owner_key, record_type="provider-key")
    assert secretish not in str(raised.value) and secretish not in repr(raised.value)


def test_fixture_matches_independent_builder_and_verifies(core, signer, owner_key):
    fixture = json.loads(FIXTURE.read_text())
    assert fixture == records_fixture()
    assert fixture["ownerKeyId"] == owner_key.key_id
    for name, record_type in (("keyRecord", "provider-key"), ("grantStateRecord", "provider-grant-state")):
        record = fixture[name]
        assert core.verify_record(record, owner_key=owner_key, record_type=record_type) == record["payload"]
        assert core.sign_record(record["payload"], signer=signer) == record


def test_new_key_id_format_and_uniqueness(core):
    ids = {core.new_key_id() for _ in range(256)}
    assert len(ids) == 256
    for value in ids:
        assert value.startswith("pk.") and len(value) == 29
        assert all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in value[3:])


def test_new_key_id_uses_128_bits_of_csprng(core, monkeypatch):
    calls = []

    def fixed(size):
        calls.append(size)
        return bytes(range(size))
    monkeypatch.setattr("secrets.token_bytes", fixed)
    value = core.new_key_id()
    assert calls == [16]
    assert value == "pk." + base64.b32encode(bytes(range(16))).decode().lower().rstrip("=")


def test_agent_uuid_requires_exact_lowercase_hex_token(core):
    token = "12345678123442348234123456789abc"
    assert core.agent_uuid(token) == AGENT
    for bad in (token.upper(), token[:-1], token + "0", "g" * 32, "", None, 7,
                AGENT, " " + token[1:], token[:-1] + "\n"):
        with pytest.raises(core.Error) as raised:
            core.agent_uuid(bad)
        assert raised.value.code == INVALID


def test_record_digest_matches_e1_grant_digest(core, signer):
    record = core.sign_record(key_payload(), signer=signer)
    assert core.record_digest(record) == hashlib.sha256(models.encode(record)).hexdigest()
    with pytest.raises(core.Error):
        core.record_digest({"x": 1.5})


def test_signature_is_checked_before_any_schema_read(core, owner_key, monkeypatch):
    from hyprial.identity.impl.provider_keys import records
    seen = []
    monkeypatch.setattr(records, "_validate", lambda payload: seen.append(payload))
    forged = raw_sign(RECORD_PURPOSE, key_payload(), private=bytes(range(100, 132)))
    with pytest.raises(core.Error) as raised:
        core.verify_record(forged, owner_key=owner_key, record_type="provider-key")
    assert raised.value.code == INVALID
    assert seen == []
