"""Record transitions (rollback, identity, tombstone) and meta/envelope agreement."""
import copy

import pytest

from hyprial.kernel import ipc_errors
from record_vectors import (
    GRANT_PURPOSE, KEY_ID, OWNER, RECIPIENT_PRIVATE, deleted_key_payload, grant_payload,
    grant_state_payload, key_payload, raw_sign, recipient_key_id, recipient_public,
)

INVALID = ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
LEGACY_GRANT = {"entryId": "openai", "fieldName": "api-key", "revision": 1}


def assert_refused(core, previous, current):
    with pytest.raises(core.Error) as raised:
        core.check_transition(previous, current)
    assert raised.value.code == INVALID


def test_first_record_and_forward_moves_accepted(core):
    core.check_transition(None, key_payload())
    core.check_transition(None, grant_state_payload())
    core.check_transition(key_payload(), key_payload(seq=2, updatedAt=1000, dekVersion=2, label="new"))
    core.check_transition(key_payload(), deleted_key_payload())
    revoked = grant_state_payload(state="revoked", grant=None, seq=2, updatedAt=2000)
    core.check_transition(grant_state_payload(), revoked)
    core.check_transition(revoked, grant_state_payload(seq=3, updatedAt=3000))
    awaiting = grant_state_payload(state="awaiting-key", keyId=None, grant=None, legacyRef=LEGACY_GRANT)
    core.check_transition(awaiting, grant_state_payload(seq=2, legacyRef={**LEGACY_GRANT, "revision": 2}))


@pytest.mark.parametrize("seq", [1, 0])
def test_equal_or_lower_seq_is_rollback(core, seq):
    assert_refused(core, key_payload(seq=2), key_payload(seq=max(seq, 1)))
    assert_refused(core, grant_state_payload(seq=2), grant_state_payload(seq=max(seq, 1)))
    revoked = grant_state_payload(state="revoked", grant=None, seq=5)
    assert_refused(core, revoked, grant_state_payload(seq=5))
    assert_refused(core, revoked, grant_state_payload(seq=4))


@pytest.mark.parametrize("name,value", [
    ("owner", "other-sub"), ("keyId", "pk." + "b" * 26), ("provider", "other"),
    ("createdAt", 999), ("legacyRef", {"entryId": "openai", "fieldName": "api-key"}),
])
def test_key_identity_fields_are_immutable(core, name, value):
    assert_refused(core, key_payload(), key_payload(seq=2, **{name: value}))


def test_key_kind_is_immutable_even_if_previous_was_forged(core):
    previous = dict(key_payload(), kind="oauth")
    assert_refused(core, previous, key_payload(seq=2))


@pytest.mark.parametrize("name,value", [
    ("owner", "other-sub"), ("agent", "12345678-1234-4234-8234-123456789abd"), ("grantId", "other"),
])
def test_grant_state_identity_fields_are_immutable(core, name, value):
    previous = grant_state_payload(state="revoked", grant=None)
    assert_refused(core, previous, grant_state_payload(state="revoked", grant=None, seq=2, **{name: value}))


def test_deleted_key_is_terminal(core):
    tombstone = deleted_key_payload()
    assert_refused(core, tombstone, key_payload(seq=3, updatedAt=3000))
    assert_refused(core, tombstone, deleted_key_payload(seq=3, updatedAt=3000))


def test_updated_at_never_decreases_and_types_never_change(core):
    assert_refused(core, key_payload(updatedAt=2000), key_payload(seq=2, updatedAt=1999))
    assert_refused(core, grant_state_payload(updatedAt=2000), grant_state_payload(seq=2, updatedAt=1999))
    assert_refused(core, key_payload(), grant_state_payload(seq=2))
    assert_refused(core, grant_state_payload(), key_payload(seq=2))


@pytest.mark.parametrize("current", [None, {}, {"type": "provider-key"}, key_payload(extra=1)])
def test_malformed_payloads_refused(core, current):
    assert_refused(core, None, current)
    assert_refused(core, key_payload(), current)


def test_inputs_are_not_mutated(core):
    previous, current = key_payload(), key_payload(seq=2)
    before = copy.deepcopy((previous, current))
    core.check_transition(previous, current)
    assert (previous, current) == before


def sealed(core, signer, **header_changes):
    header = {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM", "provider": "synthetic",
              "keyId": KEY_ID, "owner": OWNER, "dekVersion": 1, "createdAt": 1000}
    header.update(header_changes)
    recipient = core.Recipient(recipient_key_id(), recipient_public())
    signer = core.SigningKey(header["owner"], signer.private_key)
    return core.seal(b"SYNTHETIC-SECRET", header=header, recipients=[recipient], signer=signer)


def meta_for(core, envelope, **changes):
    return key_payload(envelopeDigest=core.record_digest(envelope), **changes)


def test_envelope_matches_signed_meta(core, signer, owner_key):
    envelope = sealed(core, signer)
    core.check_envelope_matches(meta_for(core, envelope), envelope)
    assert core.open_envelope(envelope, owner_key=owner_key, recipient_key_id=recipient_key_id(),
                              private_key=RECIPIENT_PRIVATE) == b"SYNTHETIC-SECRET"


@pytest.mark.parametrize("header", [
    {"keyId": "pk." + "b" * 26}, {"owner": "other-sub"}, {"provider": "other"}, {"dekVersion": 2},
])
def test_envelope_header_must_equal_meta(core, signer, header):
    envelope = sealed(core, signer, **header)
    with pytest.raises(core.Error) as raised:
        core.check_envelope_matches(meta_for(core, envelope), envelope)
    assert raised.value.code == INVALID


def test_envelope_digest_and_recipient_set_must_match(core, signer):
    envelope = sealed(core, signer)
    other = sealed(core, signer)
    with pytest.raises(core.Error):
        core.check_envelope_matches(meta_for(core, other), envelope)
    extra = {"recipientKeyId": "x25519:" + "0" * 32, "role": "owner-device", "deviceUid": "device-b"}
    meta = meta_for(core, envelope)
    for recipients in ([*meta["recipients"], extra], [extra]):
        with pytest.raises(core.Error) as raised:
            core.check_envelope_matches(dict(meta, recipients=recipients), envelope)
        assert raised.value.code == INVALID


def test_envelope_never_matches_deleted_or_malformed_meta(core, signer):
    envelope = sealed(core, signer)
    for meta in (deleted_key_payload(), None, {}, dict(meta_for(core, envelope), state="deleted")):
        with pytest.raises(core.Error) as raised:
            core.check_envelope_matches(meta, envelope)
        assert raised.value.code == INVALID
    boolean = dict(envelope, header=dict(envelope["header"], dekVersion=True))
    with pytest.raises(core.Error):
        core.check_envelope_matches(meta_for(core, boolean), boolean)
    for broken in (None, {}, {"header": {}}, [envelope]):
        with pytest.raises(core.Error) as raised:
            core.check_envelope_matches(meta_for(core, envelope), broken)
        assert raised.value.code == INVALID


def test_grant_payload_shape_reuses_e1_validator(core):
    payload = grant_state_payload(grant=raw_sign(GRANT_PURPOSE, grant_payload(dekVersion=0)))
    assert_refused(core, None, payload)


def test_active_key_dek_version_never_decreases_and_pins_its_envelope(core):
    other_digest = "f" * 64
    rotated = key_payload(seq=2, updatedAt=2000, dekVersion=2, envelopeDigest=other_digest)
    core.check_transition(key_payload(), rotated)
    assert_refused(core, rotated, key_payload(seq=3, updatedAt=3000, dekVersion=1))
    assert_refused(core, key_payload(), key_payload(seq=2, updatedAt=2000, envelopeDigest=other_digest))
    core.check_transition(rotated, key_payload(seq=3, updatedAt=3000, dekVersion=2,
                                               envelopeDigest=other_digest, label="renamed"))
