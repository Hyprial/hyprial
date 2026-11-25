"""Second batch: explicit decisions, confirmed host conflicts, bounded failure.

These pure typed fixtures establish no runtime provenance. A confirmed host
conflict may survive overall attribution unavailability (for example a missing
proxy contract); unknown attribution is not itself proof that host facts failed.
"""

import hashlib

import pytest

from .conftest import enum_value, load_golden


@pytest.mark.parametrize("overrides", [{}, {"subjectState": "unavailable"}, {"action": "read"}])
def test_decision_requires_explicit_outcome_comparison(overrides, mechanism_api):
    decision, _ = mechanism_api.decision({"set": overrides})
    with pytest.raises(TypeError):
        bool(decision)


@pytest.mark.parametrize("state,confirmed_conflict,outcome,reason", [
    ("verified", True, "deny", "SUBJECT_REJECTED"),
    ("unavailable", True, "deny", "SUBJECT_REJECTED"),
    ("unavailable", False, "unknown", "SUBJECT_UNAVAILABLE"),
])
def test_confirmed_host_conflict_precedes_missing_attribution(
    state, confirmed_conflict, outcome, reason, mechanism_api,
):
    m = mechanism_api.mechanism
    base = load_golden("decisions")["base"]
    raw = load_golden("rules")["cases"][0]["rawJson"].encode("utf-8")
    ref = m.RuleSetRef(
        scope="fixture-tailnet/scope-1", declaration_id="fixture-space/statement-1",
        version="v1", digest=hashlib.sha256(raw).hexdigest(),
        schema_version=1, subject_profile="stable-id-v1",
    )
    # A trusted synthetic producer confirms this pair independently of the
    # missing attribution/proxy prerequisite. No unverified residual pair is
    # silently promoted to evidence by this test adapter.
    subject = m.SubjectObservation(
        state=state, subject=mechanism_api.decode_subject(base["subject"]), epoch=1,
        connection_host="d.123e4567e89b42d3a456426614174000" if confirmed_conflict else None,
        attribution_host="d.223e4567e89b42d3a456426614174000" if confirmed_conflict else None,
        verification_provenance="fixture-confirmed-host-pair" if confirmed_conflict else None,
    )
    resource = m.ResourceObservation(
        state="verified", resource=base["resource"], mapping_revision="fixture-mapping-v1",
        scope=ref.scope, authority_ref=ref,
    )
    rules = m.RuleSetObservation(
        state="active", ref=ref, rules=mechanism_api.decode_rules(raw), epoch=1,
        observed_at=100.0, now=100.0, max_observation_age=30.0,
        watch_continuous=True, epoch_current=True,
    )
    decision = m.compose_evaluator().evaluate(
        subject=subject, resource=resource, action="write", ruleset_ref=None, rules=rules,
    )
    assert enum_value(decision.outcome) == outcome
    assert enum_value(decision.reason) == reason


def test_excessive_json_nesting_is_a_normal_invalid_ruleset(mechanism_api):
    # CPython 3.12's C JSON parser has a separate C recursion budget: the
    # Python recursion limit is not a reliable nesting bound. This is ~64 KiB.
    depth = 32768
    raw = (
        b'{"schemaVersion":1,"subjectProfile":"stable-id-v1","rules":'
        + b"[" * depth + b"0" + b"]" * depth + b"}"
    )
    with pytest.raises(ValueError):
        mechanism_api.decode_rules(raw)


def _explicit_fresh_inputs(mechanism_api):
    """Every unaffected observation has explicit current freshness evidence."""
    m = mechanism_api.mechanism
    base = load_golden("decisions")["base"]
    raw = load_golden("rules")["cases"][0]["rawJson"].encode("utf-8")
    ref = m.RuleSetRef(
        scope="fixture-tailnet/scope-1", declaration_id="fixture-space/statement-1",
        version="v1", digest=hashlib.sha256(raw).hexdigest(),
        schema_version=1, subject_profile="stable-id-v1",
    )
    freshness = dict(observed_at=100.0, now=100.0, max_observation_age=30.0, valid_until=150.0)
    return dict(
        subject=m.SubjectObservation(
            state="verified", subject=mechanism_api.decode_subject(base["subject"]), epoch=1,
            verification_provenance="fixture-trusted-subject", **freshness,
        ),
        resource=m.ResourceObservation(
            state="verified", resource=base["resource"], mapping_revision="fixture-mapping-v1",
            scope=ref.scope, authority_ref=ref,
            verification_provenance="fixture-trusted-resource", **freshness,
        ),
        action="write", ruleset_ref=None,
        rules=m.RuleSetObservation(
            state="active", ref=ref, rules=mechanism_api.decode_rules(raw), epoch=1,
            watch_continuous=True, epoch_current=True, **freshness,
        ),
    )


def _replace_observation(inputs, target, **changes):
    from dataclasses import replace

    return {**inputs, target: replace(inputs[target], **changes)}


def _evaluate_inputs(mechanism_api, inputs):
    return mechanism_api.mechanism.compose_evaluator().evaluate(**inputs)


def test_explicit_fresh_observations_allow(mechanism_api):
    decision = _evaluate_inputs(mechanism_api, _explicit_fresh_inputs(mechanism_api))
    assert enum_value(decision.outcome) == "allow"
    assert enum_value(decision.reason) == "MATCH"


@pytest.mark.parametrize("target,reason", [
    ("subject", "SUBJECT_UNAVAILABLE"), ("resource", "RESOURCE_UNAVAILABLE"),
])
def test_missing_all_observation_times_is_unknown(target, reason, mechanism_api):
    inputs = _replace_observation(
        _explicit_fresh_inputs(mechanism_api), target,
        observed_at=None, now=None, max_observation_age=None, valid_until=None,
    )
    decision = _evaluate_inputs(mechanism_api, inputs)
    assert enum_value(decision.outcome) == "unknown"
    assert enum_value(decision.reason) == reason


@pytest.mark.parametrize("target,reason", [
    ("subject", "SUBJECT_UNAVAILABLE"), ("resource", "RESOURCE_UNAVAILABLE"),
    ("rules", "RULESET_STALE"),
])
@pytest.mark.parametrize("field", ["observed_at", "now", "max_observation_age", "valid_until"])
def test_extreme_integer_freshness_is_typed_unknown(target, reason, field, mechanism_api):
    inputs = _replace_observation(
        _explicit_fresh_inputs(mechanism_api), target, **{field: 10**1000},
    )
    decision = _evaluate_inputs(mechanism_api, inputs)
    assert enum_value(decision.outcome) == "unknown"
    assert enum_value(decision.reason) == reason


@pytest.mark.parametrize("target,reason", [
    ("subject", "SUBJECT_UNAVAILABLE"), ("resource", "RESOURCE_UNAVAILABLE"),
])
@pytest.mark.parametrize("field,value,outcome", [
    ("observed_at", 70.0, "unknown"),  # age == max
    ("valid_until", 100.0, "unknown"),  # source expiry == now
    ("max_observation_age", 0.0, "unknown"),
    ("observed_at", 100.001, "unknown"),  # future observation
    ("observed_at", 70.001, "allow"),  # still strictly inside the age limit
])
def test_identity_resource_freshness_boundary_controls(
    target, reason, field, value, outcome, mechanism_api,
):
    inputs = _replace_observation(
        _explicit_fresh_inputs(mechanism_api), target, **{field: value},
    )
    decision = _evaluate_inputs(mechanism_api, inputs)
    assert enum_value(decision.outcome) == outcome
    assert enum_value(decision.reason) == ("MATCH" if outcome == "allow" else reason)


@pytest.mark.parametrize("target,reason", [
    ("subject", "SUBJECT_UNAVAILABLE"), ("resource", "RESOURCE_UNAVAILABLE"),
])
@pytest.mark.parametrize("field", ["observed_at", "now", "max_observation_age"])
def test_partially_missing_observation_time_controls(target, reason, field, mechanism_api):
    inputs = _replace_observation(
        _explicit_fresh_inputs(mechanism_api), target, **{field: None},
    )
    decision = _evaluate_inputs(mechanism_api, inputs)
    assert enum_value(decision.outcome) == "unknown"
    assert enum_value(decision.reason) == reason


def test_oversized_json_integer_has_private_decoder_code(mechanism_api):
    raw = (
        b'{"schemaVersion":' + b"9" * 5001
        + b',"subjectProfile":"stable-id-v1","rules":[]}'
    )
    with pytest.raises(ValueError) as caught:
        mechanism_api.decode_rules(raw)
    assert hasattr(caught.value, "code"), "decoder leaked a native ValueError"
    assert enum_value(caught.value.code) in {"PAYLOAD_INVALID", "SCHEMA_VERSION_INVALID"}


def test_existing_decoder_reason_survives_native_error_normalization(mechanism_api):
    raw = b'{"schemaVersion":2,"subjectProfile":"stable-id-v1","rules":[]}'
    with pytest.raises(ValueError) as caught:
        mechanism_api.decode_rules(raw)
    assert enum_value(caught.value.code) == "SCHEMA_VERSION_INVALID"
