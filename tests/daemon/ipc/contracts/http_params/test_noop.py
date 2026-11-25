"""Reject declarations that advertise HTTP restrictions but add no validation."""

from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from hyprial.kernel import MethodSpecError, validate_method_spec
from tests.daemon.ipc.contracts.http_params.test_constraints import declaration, http_schema
from tests.shell.request_http.constraints.test_params import entries, view
from hyprial.shell.impl.request_http.catalog import CatalogProblem


@pytest.mark.parametrize("addition", [
    {}, True, {"description": "business key"},
    {"title": "Key", "default": "k", "examples": ["k"]},
    {"allOf": [{}, {"$comment": "not validation"}]},
    {"type": ["string", "null", "number"]},
    {"type": ["string", "null", "number", "boolean"]},
    {"minLength": 0},
])
def test_noop_property_is_rejected_by_contract_and_http_catalog(addition):
    spec = declaration()
    constraints = {"type": "object", "properties": {"key": addition}}
    with pytest.raises(MethodSpecError):
        validate_method_spec(replace(spec, http_binding=replace(
            spec.http_binding, params_constraints=constraints,
        )))
    rows = entries(spec)
    rows[0]["httpBinding"]["paramsConstraints"] = constraints
    with pytest.raises(CatalogProblem) as exc:
        view(rows)
    assert exc.value.reason == "HTTP_PARAMS_INVALID"


def test_duplicate_required_does_not_count_as_new_validation():
    spec = declaration()
    schema = {**spec.params_schema, "required": ["key"]}
    with pytest.raises(MethodSpecError):
        validate_method_spec(replace(spec, params_schema=schema, http_binding=replace(
            spec.http_binding, params_constraints={"type": "object", "required": ["key"]},
        )))


def test_new_required_field_is_a_restriction_even_with_annotation_only_property():
    spec = declaration()
    spec = replace(spec, http_binding=replace(spec.http_binding, params_constraints={
        "type": "object", "properties": {"key": {
            "type": ["string", "null", "number"], "description": "required on HTTP",
        }},
        "required": ["key"],
    }))
    validator = Draft202012Validator(http_schema(spec))
    assert not validator.is_valid({})
    assert validator.is_valid({"key": "business-key"})


def test_stricter_bounds_and_types_remain_supported_without_changing_ipc():
    spec = declaration()
    for restriction, rejected, accepted in [
        ({"type": "string"}, {"key": 4}, {"key": "key"}),
        ({"type": ["string", "null", "number"], "minLength": 2}, {"key": "k"}, {"key": "key"}),
    ]:
        candidate = replace(spec, http_binding=replace(spec.http_binding, params_constraints={
            "type": "object", "properties": {"key": restriction},
        }))
        validator = Draft202012Validator(http_schema(candidate))
        assert not validator.is_valid(rejected)
        assert validator.is_valid(accepted)
        assert Draft202012Validator(dict(spec.params_schema)).is_valid(rejected)
