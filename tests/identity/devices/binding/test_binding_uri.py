"""RED/GREEN witnesses for mandatory pre-commit binding verification.

Time-claim mutations are relative to the freshly issued response. Computing
an absolute timestamp at collection can equal the claim by execution time.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hyprial.identity.interface import (
    TransportCredential,
    TransportCredentialError,
    commit_transport_credential,
    validate_enrollment_response,
)

from . import support

def _validate(staged, response, bundle):
    return validate_enrollment_response(
        response,
        staged=staged,
        pinned_trust_bundle_pem=bundle,
        effective_owner=support.OWNER,
        now=datetime.now(timezone.utc),
    )


def _credential_from(response):
    return TransportCredential(
        owner_user_id=response["ownerUserId"],
        owner=response["owner"],
        binding_id=response["bindingId"],
        device_uid=response["deviceUid"],
        credential_generation=response["credentialGeneration"],
        certificate_pem=response["certificatePem"],
        chain_pem=response["chainPem"],
        cn=response["cn"],
        spki_sha256=response["spkiSha256"],
        serial=response["serial"],
        not_before=response["notBefore"],
        not_after=response["notAfter"],
        renewable_until=response["renewableUntil"],
        trust_bundle_id=response["trustBundleId"],
        policy_issuer=response["policyIssuer"],
    )


def _assert_rejected_without_writes(tmp_path, staged, response, bundle) -> str:
    before = support.inventory(tmp_path)
    with pytest.raises(TransportCredentialError) as error:
        _validate(staged, response, bundle)
    assert support.inventory(tmp_path) == before
    with pytest.raises(TransportCredentialError):
        commit_transport_credential(
            tmp_path,
            staged=staged,
            credential=_credential_from(response),
            pinned_trust_bundle_pem=bundle,
        )
    assert support.inventory(tmp_path) == before
    return error.value.code


def _claims_for(staged, *, generation: int = 1) -> dict[str, object]:
    return support.claims_for(
        binding_id="11111111-1111-4111-8111-111111111111",
        device_uid=staged.device_uid,
        credential_generation=generation,
        spki_sha256=staged.spki_sha256,
        renewable_until=support.horizon(),
    )


def test_exact_binding_uri_alone_is_accepted(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    response, bundle = support.binding_response(
        staged, claims=claims, uri_sans=(support.reference_uri(claims),)
    )
    credential = _validate(staged, response, bundle)
    assert credential.binding_id == claims["bindingId"]
    assert credential.credential_generation == 1


def test_exact_binding_uri_plus_unrelated_uri_is_accepted(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    response, bundle = support.binding_response(
        staged,
        claims=claims,
        uri_sans=(
            support.reference_uri(claims),
            "https://unrelated.invalid/not-a-binding",
        ),
    )
    assert _validate(staged, response, bundle).spki_sha256 == staged.spki_sha256


def test_unrelated_uri_is_never_fetched(tmp_path: Path, monkeypatch) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    response, bundle = support.binding_response(
        staged,
        claims=claims,
        uri_sans=(
            support.reference_uri(claims),
            "https://unrelated.invalid/transport/binding/v9/sha256/" + "c" * 64,
        ),
    )

    def _refuse(*_args, **_kwargs):
        raise AssertionError("binding validation performed network I/O")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    assert _validate(staged, response, bundle).binding_id == claims["bindingId"]


@pytest.mark.parametrize(
    "uri_sans",
    [
        pytest.param(lambda claims: (), id="missing"),
        pytest.param(
            lambda claims: (
                support.reference_uri(claims),
                support.reference_uri(claims),
            ),
            id="duplicate-v1",
        ),
        pytest.param(
            lambda claims: (
                support.reference_uri(claims),
                support.BINDING_URI_NAMESPACE + "v2/sha256/" + "d" * 64,
            ),
            id="v1-plus-unknown-version",
        ),
        pytest.param(
            lambda claims: (
                support.BINDING_URI_NAMESPACE + "v2/sha256/" + "d" * 64,
            ),
            id="unknown-version-only",
        ),
        pytest.param(
            lambda claims: (
                support.BINDING_URI_PREFIX + support.reference_digest(claims).upper(),
            ),
            id="uppercase-digest",
        ),
        pytest.param(
            lambda claims: (support.BINDING_URI_PREFIX + "d" * 63,), id="short-digest"
        ),
        pytest.param(
            lambda claims: (support.BINDING_URI_PREFIX + "d" * 65,), id="long-digest"
        ),
        pytest.param(
            lambda claims: (support.BINDING_URI_PREFIX + "d" * 64 + "/extra",),
            id="extra-path",
        ),
        pytest.param(
            lambda claims: (
                "https://auth.hyprial.com/transport/binding/v1/" + "d" * 64,
            ),
            id="missing-sha256-segment",
        ),
        pytest.param(
            lambda claims: (
                "https://auth.hyprial.example/transport/binding/v1/sha256/"
                + "d" * 64,
            ),
            id="different-host",
        ),
        pytest.param(
            lambda claims: (support.BINDING_URI_PREFIX + "d" * 64,),
            id="mismatched-digest",
        ),
        pytest.param(
            lambda claims: (
                support.BINDING_URI_PREFIX + support.reference_digest(claims) + " ",
            ),
            id="trailing-whitespace",
        ),
    ],
)
def test_binding_uri_cardinality_and_value_are_enforced(
    tmp_path: Path, uri_sans
) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    response, bundle = support.binding_response(
        staged, claims=claims, uri_sans=uri_sans(claims)
    )
    assert _assert_rejected_without_writes(tmp_path, staged, response, bundle) == (
        "TRANSPORT_RESPONSE_INVALID"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policyIssuer", "https://authority.other.test"),
        ("ownerUserId", "u.mallory"),
        ("owner", "mallory@example.invalid"),
        ("bindingId", "33333333-3333-4333-8333-333333333333"),
        ("deviceUid", "44444444444444448444444444444444"),
        ("credentialGeneration", 2),
        ("spkiSha256", "e" * 64),
        ("renewableUntil", 100),
        ("trustBundleId", "sha256:" + "f" * 64),
    ],
    ids=(
        "policyIssuer",
        "ownerUserId",
        "owner",
        "bindingId",
        "deviceUid",
        "credentialGeneration",
        "spkiSha256",
        "renewableUntil",
        "trustBundleId",
    ),
)
def test_each_independent_claim_mutation_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    response, bundle = support.binding_response(
        staged, claims=claims, uri_sans=(support.reference_uri(claims),)
    )
    mutated = dict(response)
    mutated[field] = response[field] + value if field == "renewableUntil" else value
    assert mutated[field] != response[field]
    if field == "credentialGeneration":
        mutated["cn"] = f"hdev-{claims['bindingId']}-g{value}"
    assert _assert_rejected_without_writes(tmp_path, staged, mutated, bundle)


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (lambda response: response.__setitem__("cn", ""), "empty-cn"),
        (
            lambda response: response.__setitem__(
                "cn", "hdev-" + response["cn"].split("-", 1)[1].split("-g")[0]
            ),
            "compact-cn",
        ),
        (
            lambda response: response.__setitem__(
                "cn", response["cn"].upper()
            ),
            "uppercase-cn",
        ),
        (
            lambda response: response.__setitem__(
                "cn", response["cn"] + "x"
            ),
            "suffixed-cn",
        ),
    ],
)
def test_common_name_shape_is_enforced(tmp_path: Path, mutate, label: str) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    response, bundle = support.binding_response(
        staged, claims=claims, uri_sans=(support.reference_uri(claims),)
    )
    mutate(response)
    assert _assert_rejected_without_writes(tmp_path, staged, response, bundle) == (
        "TRANSPORT_RESPONSE_INVALID"
    )


def test_compact_common_name_matches_no_binding_id(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    compact = str(claims["bindingId"]).replace("-", "")
    response, bundle = support.binding_response(
        staged,
        claims=claims,
        uri_sans=(support.reference_uri(claims),),
        common_name=f"hdev-{compact}-g1",
    )
    assert _assert_rejected_without_writes(tmp_path, staged, response, bundle) == (
        "TRANSPORT_RESPONSE_INVALID"
    )


def test_multi_digit_generation_is_accepted_and_distinct(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged, generation=42)
    response, bundle = support.binding_response(
        staged, claims=claims, uri_sans=(support.reference_uri(claims),)
    )
    credential = _validate(staged, response, bundle)
    assert credential.credential_generation == 42
    assert credential.cn.endswith("-g42")
    single = _claims_for(staged, generation=1)
    assert support.reference_digest(claims) != support.reference_digest(single)


def test_alternate_profile_digest_is_rejected(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    alternate = dict(claims) | {"profile": "hyprial.transport.binding.v2"}
    response, bundle = support.binding_response(
        staged, claims=claims, uri_sans=(support.reference_uri(alternate),)
    )
    assert _assert_rejected_without_writes(tmp_path, staged, response, bundle) == (
        "TRANSPORT_RESPONSE_INVALID"
    )


def test_jcs_mutation_with_whitespace_separators_is_rejected(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged)
    import hashlib
    import json

    noncanonical = hashlib.sha256(
        json.dumps(claims, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    response, bundle = support.binding_response(
        staged,
        claims=claims,
        uri_sans=(support.BINDING_URI_PREFIX + noncanonical,),
    )
    assert _assert_rejected_without_writes(tmp_path, staged, response, bundle) == (
        "TRANSPORT_RESPONSE_INVALID"
    )


def test_jcs_mutation_with_ascii_escaped_unicode_is_rejected(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    claims = _claims_for(staged) | {"ownerUserId": "用户-β"}
    import hashlib
    import json

    escaped = hashlib.sha256(
        json.dumps(
            claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    response, bundle = support.binding_response(
        staged,
        claims=claims,
        uri_sans=(support.BINDING_URI_PREFIX + escaped,),
    )
    assert _assert_rejected_without_writes(tmp_path, staged, response, bundle) == (
        "TRANSPORT_RESPONSE_INVALID"
    )


def test_rejection_preserves_an_unrelated_symlink_inventory(tmp_path: Path) -> None:
    staged = support.stage(tmp_path)
    external = tmp_path / "outside.txt"
    external.write_text("not transport state", encoding="utf-8")
    (tmp_path / "secrets/transport/planted-link").symlink_to(external)
    claims = _claims_for(staged)
    response, bundle = support.binding_response(
        staged, claims=claims, uri_sans=()
    )
    assert _assert_rejected_without_writes(tmp_path, staged, response, bundle) == (
        "TRANSPORT_RESPONSE_INVALID"
    )
    assert (tmp_path / "secrets/transport/planted-link").is_symlink()
    assert external.read_text(encoding="utf-8") == "not transport state"
