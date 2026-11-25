"""Strict stable-id-v1 subject decoding and whole-segment matching."""

import pytest

from .conftest import enum_value, load_golden


@pytest.mark.parametrize("case", load_golden("subjects")["cases"], ids=lambda c: c["id"])
def test_subject_goldens(case, mechanism_api):
    operation = case["operation"]
    expected = case["expected"]
    raw = case["subject"] if operation == "decode_subject" else case["pattern"]
    decoder = (
        mechanism_api.decode_subject if operation == "decode_subject"
        else mechanism_api.decode_pattern
    )
    if not expected["valid"]:
        with pytest.raises(ValueError) as caught:
            decoder(raw)
        if "reason" in expected:
            assert enum_value(caught.value.code) == expected["reason"]
        return

    decoded = decoder(raw)
    assert decoded.canonical == raw  # No trimming, alias, case folding, or expansion.
    if operation == "match":
        subject = mechanism_api.decode_subject(case["subject"])
        assert mechanism_api.matches(decoded, subject) is expected["matches"]


def test_pattern_is_immutable(mechanism_api):
    pattern = mechanism_api.decode_pattern("*:*:*")
    with pytest.raises((AttributeError, TypeError)):
        pattern.canonical = "*:*"


def test_pattern_cannot_retain_mutable_owner_input(mechanism_api):
    owner = ["u.ou_11111111111111111111111111111111"]
    try:
        pattern = mechanism_api.mechanism.SubjectPattern(owner=owner, host="*", agent="*")
    except (ValueError, TypeError):
        return
    assert not isinstance(pattern.owner, list), "frozen container retained mutable owner"
    before = repr(pattern)
    owner.append("other-owner")
    assert repr(pattern) == before
