"""The raw token stream is validated atomically, including duplicate keys."""

import pytest

from .conftest import enum_value, load_golden


@pytest.mark.parametrize("case", load_golden("rules")["cases"], ids=lambda c: c["id"])
def test_rule_schema_goldens(case, mechanism_api):
    raw = case["rawJson"].encode("utf-8")
    expected = case["expected"]
    if not expected["valid"]:
        with pytest.raises(ValueError) as caught:
            mechanism_api.decode_rules(raw)
        if "reason" in expected:
            assert enum_value(caught.value.code) == expected["reason"]
        return

    rules = mechanism_api.decode_rules(raw)
    assert isinstance(rules, tuple), "decoded rule collection must be immutable"
    assert len(rules) == expected["ruleCount"]


def test_rule_payload_and_allow_patterns_are_immutable(mechanism_api):
    case = load_golden("rules")["cases"][0]
    rules = mechanism_api.decode_rules(case["rawJson"].encode("utf-8"))
    assert isinstance(rules[0].allow, tuple)
    with pytest.raises((AttributeError, TypeError)):
        rules[0].resource = "example:resource-2"


def test_rule_cannot_retain_mutable_allow_input(mechanism_api):
    allow = [mechanism_api.decode_pattern("*:*:*")]
    try:
        rule = mechanism_api._internal("ports").Rule(
            resource="example:resource-1", action="write", allow=allow,
        )
    except (ValueError, TypeError):
        return
    assert not isinstance(rule.allow, list), "frozen container retained mutable allow"
    before = repr(rule)
    allow.clear()
    assert repr(rule) == before
