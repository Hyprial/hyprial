"""Capability checks are distinct from matcher/evaluator behavior evidence."""

import hyprial.identity as identity
import hyprial.identity.interface as identity_interface

from .conftest import load_golden


def test_authorization_namespace_capability_exists():
    assert "AuthorizationMechanism" in identity.__all__, (
        "CAPABILITY_MISSING: approved mechanism namespace is not implemented; "
        "this is not matcher/evaluator behavior RED"
    )
    assert callable(getattr(identity.AuthorizationMechanism, "compose_evaluator", None))
    assert identity.AuthorizationMechanism is identity_interface.AuthorizationMechanism


def test_no_extra_top_level_evaluator_or_normalizer():
    forbidden = {
        "AuthorizationEvaluator", "SubjectPattern", "SubjectObservation",
        "ResourceObservation", "RuleSetRef", "RuleSetObservation", "Decision",
        "normalize_subject", "normalize_pattern", "decode_pattern", "decode_rules",
    }
    assert not (forbidden & set(identity.__all__))


def test_fixture_inventory_keeps_owner_proof_separate_from_host_residency():
    # Fixture integrity only: passing this does not prove runtime attribution.
    lifecycle = load_golden("lifecycle")
    assert lifecycle["hostBinding"]["enrollmentUidIsProof"] is False
    authorization = lifecycle["ownerAuthorizations"]["alice-for-agent-on-bob-host"]
    assert authorization["trustedKeyOwner"] == authorization["signedOwner"]
    assert authorization["signedOwner"] != lifecycle["hostBinding"]["hostOwner"]
    assert authorization["signedHost"] == lifecycle["hostBinding"]["host"]
    assert authorization["trustedKeySource"] == "fixture-owner-key-registry"
    assert {row["id"] for row in lifecycle["cases"]} >= {
        "alice-agent-on-bob-host-remains-alice", "bob-cannot-assert-alice-ownership",
        "self-provided-owner-key-is-not-authority", "same-host-wrong-agent-attribution",
        "enrollment-uid-without-host-binding", "host-mismatch-precedes-missing-proxy-contract",
    }


def test_fixture_versions_and_case_identifiers_are_consistent():
    for kind in ("subjects", "rules", "decisions", "lifecycle"):
        fixture = load_golden(kind)
        assert fixture["fixtureVersion"] == 1 and fixture["kind"] == kind
        ids = [row["id"] for row in fixture["cases"]]
        assert len(ids) == len(set(ids)), kind
