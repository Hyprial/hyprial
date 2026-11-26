"""Provider-key bytes and behaviour survive the move onto the sealed-envelope primitive.

Decision (service grant S2, 2026-11-26): provider_key_baseline.json was written
by the provider-key code at ef4f983a24, before the refactor, with fixed
randomness. The contract vector is built independently by the provider-key
tests. Both must still verify, open and reproduce byte for byte.
"""
import base64

import pytest

raw = base64.b64decode


def test_baseline_envelopes_still_verify_and_open(provider, vector, baseline, materials):
    _, owner, _ = materials
    secret = {"single": raw(baseline["single"]["secret"]), "reseal": raw(vector["secret"])}
    for name, indexes in (("single", [1]), ("reseal", [0, 1])):
        envelope = baseline[name]["envelope"]
        assert provider.verify_envelope(envelope, owner_key=owner) == envelope
        for index in indexes:
            row = vector["recipients"][index]
            assert provider.open_envelope(envelope, owner_key=owner, recipient_key_id=row["keyId"],
                                          private_key=raw(row["private"])) == secret[name]
            provider.unwrap_check(envelope, owner_key=owner, recipient_key_id=row["keyId"],
                                  private_key=raw(row["private"]))


def test_seal_reproduces_baseline_bytes(provider, baseline, materials, entropy):
    signer, _, recipients = materials
    case = baseline["single"]
    entropy(case["randomValues"])
    sealed = provider.seal(raw(case["secret"]), header=case["header"],
                           recipients=[recipients[i] for i in case["recipientIndexes"]], signer=signer)
    assert sealed == case["envelope"]


def test_reseal_reproduces_baseline_bytes(provider, vector, baseline, materials, entropy):
    signer, owner, recipients = materials
    case = baseline["reseal"]
    row = vector["recipients"][case["recipientIndex"]]
    entropy(case["randomValues"])
    sealed = provider.reseal(vector["envelope"], owner_key=owner, recipient_key_id=row["keyId"],
                             private_key=raw(row["private"]), signer=signer,
                             recipients=[recipients[i] for i in case["recipientIndexes"]],
                             created_at=case["createdAt"])
    assert sealed == case["envelope"]


def test_provider_key_binding_runs_on_the_primitive(sealed):
    from hyprial.identity.impl.provider_keys import envelope

    profile = envelope.PROFILE
    assert isinstance(profile, sealed.Profile)
    assert profile.wrap_domain == b"hyprial/provider-key/wrap/v1"
    assert profile.signature is not None
    assert profile.header_fields == frozenset(
        {"v", "alg", "wrap", "provider", "keyId", "owner", "dekVersion", "createdAt"})


def test_primitive_with_provider_profile_reproduces_contract_vector(sealed, vector, materials, entropy):
    from hyprial.identity.impl.provider_keys import envelope

    signer, owner, recipients = materials
    entropy(vector["randomValues"])
    value = sealed.seal(envelope.PROFILE, raw(vector["secret"]), header=vector["envelope"]["header"],
                        recipients=recipients, signer=signer)
    assert value == vector["envelope"]
    row = vector["recipients"][1]
    assert sealed.open(envelope.PROFILE, value, owner_key=owner, recipient_key_id=row["keyId"],
                       private_key=raw(row["private"])) == raw(vector["secret"])


def test_signed_profile_refuses_a_missing_signer(sealed, vector, materials):
    from hyprial.identity.impl.provider_keys import envelope

    _, _, recipients = materials
    with pytest.raises(envelope.Error) as caught:
        sealed.seal(envelope.PROFILE, b"synthetic", header=vector["envelope"]["header"],
                    recipients=recipients, signer=None)
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


def test_signed_profile_refuses_an_unsigned_envelope(provider, vector, materials):
    _, owner, _ = materials
    unsigned = {key: vector["envelope"][key] for key in ("header", "recipients", "payload")}
    with pytest.raises(provider.Error) as caught:
        provider.verify_envelope(unsigned, owner_key=owner)
    assert caught.value.code == "PROVIDER_KEY_SIGNATURE_INVALID"


@pytest.mark.parametrize("public", [bytes(32), bytes([1]) + bytes(31)], ids=["zero", "one"])
def test_provider_profile_keeps_its_weak_recipient_code(provider, vector, materials, public):
    from hyprial.identity.impl.user_key.encryption.crypto import key_id

    signer, _, _ = materials
    weak = provider.Recipient(key_id(public), public)
    with pytest.raises(provider.Error) as caught:
        provider.seal(b"synthetic", header=vector["envelope"]["header"], recipients=[weak], signer=signer)
    assert caught.value.code == "PROVIDER_KEY_UNSEAL_FAILED"
