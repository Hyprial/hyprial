"""Shared internal decisions: priorities, exact intersections and fail closed."""

import pytest

from .conftest import enum_value, load_golden


@pytest.mark.parametrize("case", load_golden("decisions")["cases"], ids=lambda c: c["id"])
def test_decision_goldens(case, mechanism_api):
    decision, authoritative = mechanism_api.decision(case)
    assert enum_value(decision.outcome) == case["expected"]["outcome"]
    assert enum_value(decision.reason) == case["expected"]["reason"]
    assert decision.ruleset_ref == authoritative  # Full scope/location/version/digest.
    assert decision.epoch == 1
    assert not isinstance(decision, dict)
    for wire_name in ("to_wire", "to_json", "model_dump_json", "status_code", "error_code"):
        assert not hasattr(decision, wire_name), wire_name
    assert enum_value(decision.reason) not in repr(decision)
    with pytest.raises((AttributeError, TypeError)):
        decision.outcome = "allow"


def test_equal_digest_other_scope_is_not_the_same_reference(mechanism_api):
    m = mechanism_api.mechanism
    common = dict(
        declaration_id="fixture-statement", version="opaque-v1", digest="1" * 64,
        schema_version=1, subject_profile="stable-id-v1",
    )
    assert m.RuleSetRef(scope="space-a", **common) != m.RuleSetRef(scope="space-b", **common)


def test_reference_cannot_retain_mutable_version_input(mechanism_api):
    version = [["opaque-v1"]]
    try:
        ref = mechanism_api.mechanism.RuleSetRef(
            scope="space-a", declaration_id="fixture-statement", version=version,
            digest="1" * 64, schema_version=1, subject_profile="stable-id-v1",
        )
    except (ValueError, TypeError):
        return
    assert not isinstance(ref.version, list), "frozen container retained mutable version"
    if isinstance(ref.version, tuple):
        assert not any(isinstance(item, (list, dict, set)) for item in ref.version)
    before = repr(ref)
    version[0].append("opaque-v2")
    assert repr(ref) == before
