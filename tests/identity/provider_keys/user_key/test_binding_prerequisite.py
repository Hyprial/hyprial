"""U2 only verifies locally supplied, synthetic issuer documents."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity import UserKey
from hyprial.daemon import UserKey as DaemonUserKey


def binding(issuer, **changes):
    claims = dict(issuer.binding, **changes)
    return UserKey.verify_binding(issuer.token(claims), issuer=issuer.binding["iss"],
                                  keys=issuer.keys, now=issuer.now)


def port(issuer, **overrides):
    args = dict(issuer=issuer.binding["iss"], keys=lambda: issuer.keys,
                subject=lambda owner: "test-sub", clock=lambda: issuer.now,
                binding=lambda owner, kid: issuer.token(issuer.binding),
                revocations=lambda sub: issuer.token(issuer.revocations, "hyprial-user-key-revocations+jwt"))
    args.update(overrides)
    return DaemonUserKey.OwnerKeyPort(**args)


def lookup(issuer, verifier=None):
    return (verifier or port(issuer)).lookup("alice@test.invalid", issuer.binding["keyId"])


def test_valid_binding_and_revocation_are_verified_with_anchor(issuer):
    result = lookup(issuer)
    assert result.state == "verified"
    assert result.public_key == issuer.binding["publicKey"]
    assert result.device_uid == "test-device"
    assert result.expires_at == issuer.revocations["exp"]
    assert result.anchor_ref["iss"] == issuer.binding["iss"]
    assert result.anchor_ref["jwksKid"] == "test-rsa"
    assert result.anchor_ref["revocationSeq"] == 1
    assert len(result.anchor_ref["credentialSha256"]) == 64


@pytest.mark.parametrize("changes", [
    {"iss": "https://wrong.invalid"}, {"aud": "legacy-assertion"},
    {"owner": "other-tenant"}, {"keyAlg": "X25519"}, {"keyId": "ed25519:wrong"},
    {"publicKey": "invalid"}, {"deviceUid": ""}, {"extra": "SENSITIVE-SENTINEL"},
    {"iat": True}, {"nbf": 1002}, {"exp": 1001}, {"iat": 1002},
    {"exp": 1000 + 31 * 86400}, {"nbf": 999}, {"sub": ""}, {"jti": []},
])
def test_binding_claims_strict(issuer, changes):
    with pytest.raises(UserKey.Error) as caught:
        binding(issuer, **changes)
    assert "SENSITIVE-SENTINEL" not in str(caught.value)


@pytest.mark.parametrize("typ", ["JWT", "hyprial-user-key-revocations+jwt", "", None])
def test_binding_typ_cannot_be_identity_or_revocation(issuer, typ):
    token = issuer.token(issuer.binding, typ)
    with pytest.raises(UserKey.Error):
        UserKey.verify_binding(token, issuer=issuer.binding["iss"], keys=issuer.keys, now=issuer.now)


@pytest.mark.parametrize("header", [{"alg": "HS256"}, {"alg": "none"},
    {"kid": "unknown"}, {"jku": "https://attacker.invalid"}, {"crit": ["x"]}])
def test_untrusted_header_cannot_select_keys_or_algorithm(issuer, header):
    token = issuer.token(issuer.binding, **header)
    with pytest.raises(UserKey.Error):
        UserKey.verify_binding(token, issuer=issuer.binding["iss"], keys=issuer.keys, now=issuer.now)


@pytest.mark.parametrize("changes", [
    {"seq": True}, {"seq": -1}, {"seq": 1.5}, {"iat": 1002}, {"exp": 1001},
    {"exp": 1000 + 86401}, {"sub": ""}, {"revokedKeyIds": ["not-a-key"]},
    {"revokedKeyIds": "ed25519:fake"}, {"extra": "SENSITIVE-SENTINEL"},
])
def test_revocations_strict(issuer, changes):
    token = issuer.token(dict(issuer.revocations, **changes), "hyprial-user-key-revocations+jwt")
    with pytest.raises(UserKey.Error):
        UserKey.verify_revocations(token, issuer=issuer.binding["iss"], keys=issuer.keys, now=issuer.now)


def test_revocation_has_no_old_signature_grace(issuer):
    verifier = port(issuer)
    assert lookup(issuer, verifier).state == "verified"
    issuer.revocations.update(seq=2, revokedKeyIds=[issuer.binding["keyId"]])
    assert lookup(issuer, verifier).state == "absent"


@pytest.mark.parametrize("field,value", [("sub", "other-sub"), ("name", "bob@test.invalid")])
def test_verified_other_user_is_positive_absence(issuer, field, value):
    issuer.binding[field] = value
    assert lookup(issuer).state == "absent"


@pytest.mark.parametrize("failure", ["keys", "binding", "revocations", "subject"])
def test_source_outage_is_unavailable_not_absent(issuer, failure):
    def unavailable(*args):
        raise OSError("SENSITIVE-SENTINEL")
    result = lookup(issuer, port(issuer, **{failure: unavailable}))
    assert result.state == "unavailable"
    assert result.public_key is None
    assert "SENSITIVE-SENTINEL" not in repr(result)


@pytest.mark.parametrize("failure", ["binding", "revocations", "subject"])
def test_missing_source_is_unavailable(issuer, failure):
    assert lookup(issuer, port(issuer, **{failure: lambda *args: None})).state == "unavailable"


def test_expired_binding_and_revocation_staleness_fail_closed(issuer):
    verifier = port(issuer)
    assert lookup(issuer, verifier).state == "verified"
    issuer.now = issuer.revocations["exp"]
    assert lookup(issuer, verifier).state == "unavailable"
    issuer.revocations.update(iat=issuer.now, exp=issuer.now + 86400, seq=2)
    assert lookup(issuer, verifier).state == "verified"
    issuer.now = issuer.binding["exp"]
    assert lookup(issuer, verifier).state == "unavailable"


def test_sequence_rollback_or_conflict_does_not_resurrect(issuer):
    verifier = port(issuer)
    issuer.revocations.update(seq=3, revokedKeyIds=[issuer.binding["keyId"]])
    assert lookup(issuer, verifier).state == "absent"
    issuer.revocations.update(seq=2, revokedKeyIds=[])
    assert lookup(issuer, verifier).state == "unavailable"
    issuer.revocations["seq"] = 3
    assert lookup(issuer, verifier).state == "unavailable"
    issuer.revocations["seq"] = 4
    assert lookup(issuer, verifier).state == "unavailable"


def test_self_reported_public_key_is_never_a_trust_source(issuer):
    verifier = port(issuer, keys=lambda: {}, binding=lambda *args: json.dumps(issuer.binding))
    assert lookup(issuer, verifier).state == "unavailable"
    assert DaemonUserKey.OwnerKeyPort().lookup("alice@test.invalid", issuer.binding["keyId"]).state == "unavailable"


def test_local_signer_trust_requires_device_and_public_key_match(issuer, tmp_path):
    store = UserKey.Store(state_dir=tmp_path / "state", owner="alice@test.invalid",
                          casdoor_sub="test-sub", device_uid="test-device", fingerprint="machine")
    status = store.ensure()
    issuer.binding.update(keyId=status["keyId"], publicKey=status["publicKey"])
    verifier = port(issuer)
    record = store.load_for_signing()
    trust = verifier.trust(record)
    assert trust.public_key == status["publicKey"]
    issuer.binding["deviceUid"] = "forged-device"
    with pytest.raises(UserKey.Error):
        verifier.trust(record)
    issuer.binding["deviceUid"] = "test-device"
    issuer.revocations.update(seq=2, revokedKeyIds=[status["keyId"]])
    with pytest.raises(UserKey.Error):
        verifier.trust(record)


def test_verified_wrong_owner_is_positive_absence_without_revocation_source(issuer):
    issuer.binding["name"] = "somebody-else"
    verifier = port(issuer, revocations=None)
    assert lookup(issuer, verifier).state == "absent"


def test_signed_document_cache_is_reverified_and_expires(issuer):
    verifier = port(issuer)
    assert lookup(issuer, verifier).state == "verified"
    verifier.binding = verifier.revocations = lambda *args: None
    assert lookup(issuer, verifier).state == "verified"
    issuer.now = issuer.revocations["exp"]
    assert lookup(issuer, verifier).state == "unavailable"


def test_fallback_order_and_invalid_document_does_not_mask_with_cache(issuer):
    calls = []
    def missing(*args):
        calls.append("directory")
        return None
    def remote(*args):
        calls.append("remote")
        return issuer.token(issuer.binding)
    verifier = port(issuer, binding=missing, binding_fallback=remote)
    assert lookup(issuer, verifier).state == "verified"
    assert calls == ["directory", "remote"]
    verifier.binding = lambda *args: "corrupt"
    verifier.binding_fallback = None
    assert lookup(issuer, verifier).state == "unavailable"


@pytest.mark.parametrize("now", [True, float("nan"), float("inf"), 10**1000, -1])
def test_invalid_clock_is_sanitized(issuer, now):
    with pytest.raises(UserKey.Error):
        UserKey.verify_binding(issuer.token(issuer.binding), issuer=issuer.binding["iss"], keys=issuer.keys, now=now)


def test_protected_user_keys_reserved_but_no_author_is_implicitly_trusted():
    from hyprial.identity import protected_directory_doc_id, parse_protected_directory_doc_id
    from hyprial.identity import protected_directory_author_allowed
    path = "directory/user-keys/alice/revocations.jwt"
    doc_id = protected_directory_doc_id("synthetic-space", path, space_owner="alice")
    assert doc_id is not None, "user-key records must not enter the ordinary unsigned partition"
    assert parse_protected_directory_doc_id(doc_id) == (
        "synthetic-space", "user-keys", "user:alice", "revocations.jwt")
    for author in ("alice", "bob", "user:alice"):
        assert not protected_directory_author_allowed(section="user-keys", path_principal="user:alice", author=author)


def test_production_publication_is_explicitly_unavailable():
    with pytest.raises(UserKey.Error) as caught:
        DaemonUserKey.publish()
    assert caught.value.code == "USER_KEY_UNBOUND"


def test_binding_fixture_verifies_with_only_public_test_issuer():
    from cryptography.hazmat.primitives.asymmetric import rsa
    fixture = json.loads(Path("contract/fixtures/user-key/bindings.json").read_text())
    assert fixture["status"] == "synthetic-local-verification-only"
    keys = {fixture["kid"]: rsa.RSAPublicNumbers(fixture["e"], int(fixture["n"])).public_key()}
    result = UserKey.verify_binding(fixture["binding"], issuer=fixture["issuer"], keys=keys, now=fixture["now"])
    assert result.public_key == fixture["publicKey"]
    with pytest.raises(UserKey.Error):
        UserKey.verify_binding(fixture["badSignature"], issuer=fixture["issuer"], keys=keys, now=fixture["now"])
