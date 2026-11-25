"""HTTP constraints share the listener/registry/mock FormatChecker profile."""

from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from hyprial.kernel import project_http_params_schema
from tests.daemon.ipc.contracts.http_params.test_constraints import declaration, http_schema

VALID_UUID = "f94d0a33-7b35-4c66-bd3c-25b218813889"


def uuid_declaration(*, require_key=False):
    spec = declaration()
    # Restate the existing type for MethodSpec's schema profile. The format is
    # the only new property restriction; the original IPC schema has no format.
    constraints = {"type": "object", "properties": {"key": {
        "type": ["string", "null", "number"], "format": "uuid",
    }}}
    if require_key:
        constraints["required"] = ["key"]
    return replace(spec, http_binding=replace(spec.http_binding, params_constraints=constraints))


def test_format_only_projection_is_an_executable_restriction():
    original = {"type": "object", "properties": {"key": {"type": "string"}}}
    projected = project_http_params_schema(original, {
        "type": "object", "properties": {"key": {"format": "uuid"}},
    })
    validator = Draft202012Validator(projected, format_checker=FormatChecker())
    assert not validator.is_valid({"key": "not-a-uuid"})
    assert validator.is_valid({"key": VALID_UUID})
    assert Draft202012Validator(original, format_checker=FormatChecker()).is_valid({"key": "not-a-uuid"})


def test_unknown_format_is_annotation_not_a_new_constraint():
    unknown = "hyprial-unregistered-test-format"
    assert unknown not in FormatChecker().checkers
    with pytest.raises(ValueError, match="must add validation"):
        project_http_params_schema({"type": "object", "properties": {"key": {"type": "string"}}}, {
            "type": "object", "properties": {"key": {"format": unknown}},
        })


def test_repeating_an_existing_format_is_still_a_noop():
    with pytest.raises(ValueError, match="must add validation"):
        project_http_params_schema({"type": "object", "properties": {"key": {
            "type": "string", "format": "uuid",
        }}}, {"type": "object", "properties": {"key": {"format": "uuid"}}})


def test_format_is_the_only_new_rule_in_the_exported_method_schema():
    validator = Draft202012Validator(http_schema(uuid_declaration()), format_checker=FormatChecker())
    assert not validator.is_valid({"key": "not-a-uuid"})
    assert validator.is_valid({"key": VALID_UUID})
    assert validator.is_valid({})
