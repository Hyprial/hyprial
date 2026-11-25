"""Frozen RFC 8785 vectors plus supplemental claim and encoding negatives."""

from __future__ import annotations

import hashlib
import json

import pytest

from hyprial.identity.interface import TransportCredentialError

from . import support


def _product():
    from hyprial.identity.impl.device.binding import (
        binding_claims,
        binding_digest,
        binding_uri,
        canonical_binding_bytes,
    )

    return binding_claims, canonical_binding_bytes, binding_digest, binding_uri


def test_frozen_vector_file_is_byte_identical() -> None:
    raw = (support.FIXTURES / "issuer-binding-profile-v3-vectors.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == support.FROZEN_VECTORS_SHA256


def test_reference_encoder_reproduces_the_three_frozen_vectors() -> None:
    vectors = support.frozen_vectors()["vectors"]
    assert [vector["id"] for vector in vectors] == ["vector1", "vector2", "vector3"]
    for vector in vectors:
        claims = vector["input"]
        assert list(claims) == list(claims)
        assert support.reference_canonical_bytes(claims).hex() == vector[
            "canonicalBytesHex"
        ]
        assert support.reference_digest(claims) == vector["sha256"]
        assert support.reference_uri(claims) == vector["uri"]


def test_product_encoder_reproduces_the_three_frozen_vectors_byte_for_byte() -> None:
    _binding_claims, canonical_binding_bytes, binding_digest, binding_uri = _product()
    for vector in support.frozen_vectors()["vectors"]:
        claims = vector["input"]
        assert canonical_binding_bytes(claims).hex() == vector["canonicalBytesHex"]
        assert binding_digest(claims) == vector["sha256"]
        assert binding_uri(claims) == vector["uri"]


def test_vector_two_and_three_remain_distinct_without_normalization() -> None:
    vectors = {vector["id"]: vector for vector in support.frozen_vectors()["vectors"]}
    two = vectors["vector2"]
    three = vectors["vector3"]
    assert two["input"]["owner"].encode() == "élise@example.invalid".encode()
    assert three["input"]["owner"].encode() == (
        "e\u0301lise@example.invalid".encode()
    )
    assert two["canonicalBytesHex"] != three["canonicalBytesHex"]
    assert two["sha256"] != three["sha256"]
    assert two["uri"] != three["uri"]


def test_property_order_does_not_change_the_canonical_bytes() -> None:
    _binding_claims, canonical_binding_bytes, binding_digest, _binding_uri = _product()
    vector = support.frozen_vectors()["vectors"][0]
    shuffled = dict(reversed(list(vector["input"].items())))
    assert canonical_binding_bytes(shuffled).hex() == vector["canonicalBytesHex"]
    assert binding_digest(shuffled) == vector["sha256"]


def test_claim_builder_binds_response_and_device_values() -> None:
    binding_claims, _canonical, _digest, _uri = _product()
    claims = binding_claims(
        policy_issuer=support.POLICY_ISSUER,
        binding_id="11111111-1111-4111-8111-111111111111",
        owner_user_id=support.OWNER_USER_ID,
        owner=support.OWNER,
        device_uid="22222222222242228222222222222222",
        credential_generation=1,
        spki_sha256="a" * 64,
        renewable_until=1792567200,
        trust_bundle_id=support.TRUST_BUNDLE_ID,
    )
    assert tuple(claims) == support.CLAIM_FIELDS
    assert claims["profile"] == support.PROFILE


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda claims: claims.__setitem__("profile", "hyprial.transport.binding.v2"), id="profile"),
        pytest.param(lambda claims: claims.__setitem__("credentialGeneration", True), id="bool-generation"),
        pytest.param(lambda claims: claims.__setitem__("credentialGeneration", 1.0), id="float-generation"),
        pytest.param(lambda claims: claims.__setitem__("credentialGeneration", "1"), id="string-generation"),
        pytest.param(lambda claims: claims.__setitem__("credentialGeneration", 0), id="zero-generation"),
        pytest.param(lambda claims: claims.__setitem__("credentialGeneration", 2**31), id="generation-overflow"),
        pytest.param(lambda claims: claims.__setitem__("renewableUntil", -1), id="negative-horizon"),
        pytest.param(
            lambda claims: claims.__setitem__("renewableUntil", support.MAX_SAFE_INTEGER + 1),
            id="horizon-overflow",
        ),
        pytest.param(lambda claims: claims.__setitem__("renewableUntil", 1.0), id="float-horizon"),
        pytest.param(lambda claims: claims.__setitem__("spkiSha256", "A" * 64), id="uppercase-spki"),
        pytest.param(lambda claims: claims.__setitem__("spkiSha256", "a" * 63), id="short-spki"),
        pytest.param(
            lambda claims: claims.__setitem__("bindingId", "11111111111141118111111111111111"),
            id="compact-binding-id",
        ),
        pytest.param(
            lambda claims: claims.__setitem__("bindingId", "11111111-1111-1111-8111-111111111111"),
            id="uuid-version-one",
        ),
        pytest.param(
            lambda claims: claims.__setitem__("deviceUid", "22222222-2222-4222-8222-222222222222"),
            id="hyphenated-device-uid",
        ),
        pytest.param(lambda claims: claims.__setitem__("owner", ""), id="empty-owner"),
        pytest.param(lambda claims: claims.__setitem__("ownerUserId", 42), id="non-string-owner-user-id"),
        pytest.param(lambda claims: claims.__setitem__("extra", "value"), id="extra-key"),
        pytest.param(lambda claims: claims.pop("owner", None), id="missing-key"),
        pytest.param(lambda claims: claims.__setitem__("owner", "bad\ud800"), id="lone-surrogate"),
    ],
)
def test_malformed_claim_profiles_are_rejected(mutate) -> None:
    _binding_claims, canonical_binding_bytes, _digest, _uri = _product()
    claims = dict(support.frozen_vectors()["vectors"][0]["input"])
    mutate(claims)
    with pytest.raises(TransportCredentialError) as error:
        canonical_binding_bytes(claims)
    assert error.value.code == "TRANSPORT_RESPONSE_INVALID"


@pytest.mark.parametrize("generation", [2, 9, 10, 11, 99, 100, 2**31 - 1])
def test_multi_digit_generations_encode_as_integers(generation: int) -> None:
    _binding_claims, canonical_binding_bytes, binding_digest, _uri = _product()
    claims = dict(support.frozen_vectors()["vectors"][0]["input"])
    claims["credentialGeneration"] = generation
    encoded = canonical_binding_bytes(claims)
    assert f'"credentialGeneration":{generation},'.encode() in encoded
    assert b'"credentialGeneration":' + str(generation).encode() in encoded
    assert binding_digest(claims) == hashlib.sha256(encoded).hexdigest()
    assert binding_digest(claims) == support.reference_digest(claims)


def test_duplicate_keys_are_rejected_before_canonicalization() -> None:
    _binding_claims, canonical_binding_bytes, _digest, _uri = _product()
    vector = support.frozen_vectors()["vectors"][0]
    duplicate = json.loads(json.dumps(vector["input"]))
    duplicate["__duplicate__"] = duplicate["owner"]
    with pytest.raises(TransportCredentialError):
        canonical_binding_bytes(duplicate)
