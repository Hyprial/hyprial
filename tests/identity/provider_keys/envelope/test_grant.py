"""Pure grant signature, exact binding and caller-supplied replay fence checks."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from hyprial.identity import UserKey

AGENT = "12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def grant_vector():
    return json.loads((Path(__file__).resolve().parents[4] / "contract/fixtures/provider-keys/grants.json").read_text())


def signed(core, vector, materials, **updates):
    payload = {"v": 1, "provider": "synthetic", "keyId": "pk_test", "owner": "test-sub",
               "agent": AGENT, "dekVersion": 1, "recipientKeyId": vector["recipients"][0]["keyId"],
               "issuedAt": 1000, "expiresAt": 3000}
    return core.sign_grant({**payload, **updates}, signer=materials[0])


def verify(core, grant, vector, materials, **updates):
    params = dict(owner_key=materials[1], header=vector["envelope"]["header"], agent=AGENT,
                  recipient_key_id=vector["recipients"][0]["keyId"], now=2000)
    return core.verify_grant(grant, **{**params, **updates})


def access(core, signed_grant, vector, materials, **updates):
    params = dict(envelope=vector["envelope"], grant=signed_grant, owner_key=materials[1],
                  agent=AGENT, recipient_key_id=vector["recipients"][0]["keyId"], now=2000,
                  current_grant_digest=UserKey.digest(signed_grant), current_dek_version=1, revoked=False)
    return core.check_access(**{**params, **updates})


def test_grant_matches_fixed_independent_vector(core, vector, grant_vector, materials):
    actual = signed(core, vector, materials)
    assert actual == grant_vector["grant"]
    payload = actual["payload"]
    raw = b"hyprial/provider-key/grant/v1\0" + hashlib.sha256(UserKey.canonical_bytes(payload)).digest()
    assert base64.b64encode(raw).decode() == grant_vector["signingInput"]
    assert verify(core, actual, vector, materials) == payload
    assert access(core, actual, vector, materials) == payload


@pytest.mark.parametrize("field", ["v", "provider", "keyId", "owner", "agent", "dekVersion", "recipientKeyId", "issuedAt", "expiresAt"])
def test_grant_field_tamper_fails(core, vector, materials, field):
    grant = signed(core, vector, materials)
    old = grant["payload"][field]
    grant["payload"][field] = old + 1 if type(old) is int else old + "-tampered"
    with pytest.raises(core.Error) as caught:
        verify(core, grant, vector, materials)
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


@pytest.mark.parametrize("field,value", [
    ("provider", "another-provider"), ("keyId", "pk_another"), ("dekVersion", 2),
    ("agent", "22345678-1234-4234-8234-123456789abc"),
    ("recipientKeyId", "x25519:" + "f" * 32), ("issuedAt", 2001), ("expiresAt", 2000),
])
def test_valid_signatures_do_not_override_expected_context(core, vector, materials, field, value):
    grant = signed(core, vector, materials, **{field: value})
    with pytest.raises(core.Error):
        verify(core, grant, vector, materials)


@pytest.mark.parametrize("updates", [{"v": True}, {"v": 2}, {"issuedAt": False}, {"issuedAt": -1},
                                     {"expiresAt": 999}, {"expiresAt": None}, {"agent": "name-not-uuid"},
                                     {"revision": 1}, {"revoked": False}])
def test_grant_schema_is_exact(core, vector, materials, updates):
    with pytest.raises(core.Error):
        signed(core, vector, materials, **updates)


def test_optional_expiry_and_exact_time_boundary(core, vector, materials):
    grant = signed(core, vector, materials)
    assert verify(core, grant, vector, materials, now=1000)
    assert verify(core, grant, vector, materials, now=2999)
    for now in (999, 3000, True, -1):
        with pytest.raises(core.Error):
            verify(core, grant, vector, materials, now=now)
    payload = grant["payload"]
    payload.pop("expiresAt")
    unlimited = core.sign_grant(payload, signer=materials[0])
    assert verify(core, unlimited, vector, materials, now=999999)


def test_grant_owner_and_key_kind_are_not_replaceable(core, vector, materials):
    other_signer = core.SigningKey("other-owner", b"z" * 32)
    payload = signed(core, vector, materials)["payload"]
    with pytest.raises(core.Error):
        core.sign_grant(payload, signer=other_signer)
    forged = core.sign_grant({**payload, "owner": "other-owner"}, signer=other_signer)
    with pytest.raises(core.Error):
        verify(core, forged, vector, materials)
    device = core.OwnerKey("test-sub", vector["ownerKeyId"], base64.b64decode(vector["ownerPublic"]), key_kind="device")
    with pytest.raises(core.Error):
        verify(core, signed(core, vector, materials), vector, materials, owner_key=device)


def test_grant_cannot_use_envelope_purpose(core, vector, materials):
    grant = signed(core, vector, materials)
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(vector["ownerPrivate"]))
    raw = b"hyprial/provider-key/envelope/v1\0" + hashlib.sha256(UserKey.canonical_bytes(grant["payload"])).digest()
    grant["sealedBy"]["sig"] = base64.b64encode(key.sign(raw)).decode()
    with pytest.raises(core.Error):
        verify(core, grant, vector, materials)


def test_replay_of_old_signed_grant_or_dek_version_is_refused(core, vector, materials):
    old = signed(core, vector, materials)
    newer = signed(core, vector, materials, issuedAt=1500)
    with pytest.raises(core.Error):
        access(core, old, vector, materials, current_grant_digest=UserKey.digest(newer))
    with pytest.raises(core.Error):
        access(core, old, vector, materials, current_dek_version=2)


@pytest.mark.parametrize("updates,code", [
    ({"grant": None}, "PROVIDER_GRANT_MISSING"),
    ({"revoked": True}, "PROVIDER_GRANT_REVOKED"),
    ({"envelope": None}, "PROVIDER_KEY_NOT_FOUND"),
])
def test_failure_precedence_uses_explicit_caller_facts(core, vector, materials, updates, code):
    with pytest.raises(core.Error) as caught:
        access(core, signed(core, vector, materials), vector, materials, **updates)
    assert caught.value.code == code


def test_grant_does_not_create_a_missing_recipient(core, vector, materials):
    # An authentic grant may name a not-yet-sealed device. It cannot add a wrap.
    grant = signed(core, vector, materials, recipientKeyId="x25519:" + "a" * 32)
    with pytest.raises(core.Error) as caught:
        access(core, grant, vector, materials, recipient_key_id="x25519:" + "a" * 32)
    assert caught.value.code == "PROVIDER_KEY_RECIPIENT_PENDING"


def test_verification_returns_no_mutable_alias(core, vector, materials):
    grant = signed(core, vector, materials)
    prior = deepcopy(grant)
    payload = verify(core, grant, vector, materials)
    payload["owner"] = "changed"
    assert grant == prior


def test_replay_fence_and_signature_use_one_detached_candidate(core, vector, materials, monkeypatch):
    from hyprial.identity.impl.provider_keys import policy

    old = signed(core, vector, materials)
    latest = signed(core, vector, materials, issuedAt=1500)
    original_verify = policy.verify_grant

    def concurrent_replacement(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        # Simulate the caller updating its shared candidate after verification.
        # The verifier must not combine old verified payload with a new digest.
        old.clear()
        old.update(latest)
        return result

    monkeypatch.setattr(policy, "verify_grant", concurrent_replacement)
    with pytest.raises(core.Error) as caught:
        access(core, old, vector, materials, current_grant_digest=UserKey.digest(latest))
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


def test_failure_fixture_is_executable(core, vector, materials):
    from hyprial.kernel import ipc_errors

    path = Path(__file__).resolve().parents[4] / "contract/fixtures/provider-keys/failures.json"
    fixture = json.loads(path.read_text())
    assert set(fixture["codes"]) <= ipc_errors.ALL_CODES
    for case in fixture["accessPrecedence"]:
        grant = signed(core, vector, materials)
        with pytest.raises(core.Error) as caught:
            access(core, grant, vector, materials, grant=None if case["grantMissing"] else grant,
                   revoked=case["revoked"], envelope=None if case["envelopeMissing"] else vector["envelope"])
        assert caught.value.code == case["code"], case["name"]


def _second_owner_key(core, owner="test-sub"):
    from hyprial.identity.impl.user_key.crypto import key_id

    private = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    public = private.public_key().public_bytes_raw()
    return core.SigningKey(owner, bytes(range(1, 33))), core.OwnerKey(owner, key_id(public), public)


def test_a_grant_signed_by_another_owner_key_verifies_with_its_own_key(core, vector, materials):
    signer, grant_key = _second_owner_key(core)
    grant = core.sign_grant(signed(core, vector, materials)["payload"], signer=signer)
    assert access(core, grant, vector, materials, grant_owner_key=grant_key) == grant["payload"]
    with pytest.raises(core.Error) as caught:
        access(core, grant, vector, materials)
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


def test_the_grant_key_never_verifies_the_envelope(core, vector, materials):
    _, grant_key = _second_owner_key(core)
    with pytest.raises(core.Error) as caught:
        access(core, signed(core, vector, materials), vector, materials, owner_key=grant_key,
               grant_owner_key=materials[1])
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


def test_a_grant_key_of_another_owner_is_refused(core, vector, materials):
    _, foreign = _second_owner_key(core, owner="other-sub")
    with pytest.raises(core.Error) as caught:
        access(core, signed(core, vector, materials), vector, materials, grant_owner_key=foreign)
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"
