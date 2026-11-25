"""HTTP-only narrowing must not change legacy IPC validation or error order."""

from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import (
    DaemonRequestError, HttpBinding, IdentityBinding, MethodError, MethodSpec,
    MethodSpecError, export_catalog, export_openapi, validate_method_spec,
)


def declaration(**overrides):
    values = dict(
        name="probe.submit", owner="daemon.probe", mutates=True, retry="never",
        params_schema={
            "type": "object", "properties": {
                "key": {"type": ["string", "null", "number"]},
                "summary": {"type": ["string", "null", "number"]},
                "sessionRef": {"type": "string", "x-hyprial-http-forbidden": True},
            }, "additionalProperties": False,
        },
        result_schema={"type": "object", "properties": {"ok": {"const": True}},
                       "required": ["ok"], "additionalProperties": False},
        errors=(MethodError("SUMMARY_REQUIRED"), MethodError("INVALID_ARGUMENT")),
        http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none"), params_constraints={
            "type": "object", "properties": {"key": {
                "type": "string", "minLength": 1, "pattern": r"\S",
            }}, "required": ["key"],
        }),
    )
    return MethodSpec(**(values | overrides))


def http_schema(spec):
    return export_openapi((spec,))["paths"]["/daemon/request/probe.submit"]["post"][
        "requestBody"]["content"]["application/json"]["schema"]


def test_optional_ipc_key_is_required_nonblank_string_only_over_http():
    spec = declaration()
    ipc = export_catalog((spec,))["methods"][0]["paramsSchema"]
    schema = http_schema(spec)
    assert Draft202012Validator(ipc).is_valid({})
    assert Draft202012Validator(ipc).is_valid({"key": None})
    for params in ({}, {"key": None}, {"key": 123}, {"key": ""}, {"key": " \t"}):
        assert not Draft202012Validator(schema).is_valid(params)
    assert Draft202012Validator(schema).is_valid({"key": "business-key"})
    assert "sessionRef" not in schema["properties"]
    assert not Draft202012Validator(schema).is_valid({"key": "k", "sessionRef": "s"})


def test_ipc_handler_error_and_no_key_behavior_remain_authoritative():
    effects = []

    def handler(app, params, **kwargs):
        if not isinstance(params.get("summary"), str) or not params["summary"].strip():
            raise DaemonRequestError("SUMMARY_REQUIRED", "summary required")
        effects.append(dict(params))
        return {"ok": True}

    registry = MethodRegistry.from_bindings(((declaration(), handler),))
    for params in ({}, {"summary": None}, {"summary": 3}, {"summary": " "}):
        with pytest.raises(DaemonRequestError) as exc:
            registry.invoke(None, "probe.submit", params)
        assert exc.value.code == "SUMMARY_REQUIRED"
    assert effects == []
    assert registry.invoke(None, "probe.submit", {"summary": "hello"}) == {"ok": True}
    assert effects == [{"summary": "hello"}]


def test_constraints_intersect_the_original_schema_never_replace_it():
    spec = declaration()
    binding = replace(spec.http_binding, params_constraints={
        "type": "object", "properties": {"key": {"type": "boolean"}},
    })
    schema = http_schema(replace(spec, http_binding=binding))
    assert not Draft202012Validator(schema).is_valid({"key": True})
    assert not Draft202012Validator(schema).is_valid({"key": "string"})
    assert Draft202012Validator(schema).is_valid({})


@pytest.mark.parametrize("constraints", [
    {}, {"type": "array"}, {"type": "object", "required": ["missing"]},
    {"type": "object", "required": ["sessionRef"]},
    {"type": "object", "properties": {"sessionRef": {"minLength": 2}}},
    {"type": "object", "properties": {"newField": {"type": "string"}}},
    {"type": "object", "additionalProperties": True},
    {"type": "object", "required": "key"},
    {"type": "object", "required": ["key", "key"]},
    {"type": "object", "properties": {"key": {"type": "bogus"}}},
    {"type": "object", "properties": {"key": {"$ref": "#/properties/key"}}},
    {"type": "object", "properties": {"key": {"$ref": "https://invalid.test/schema"}}},
])
def test_invalid_or_undeclared_constraints_fail_closed(constraints):
    with pytest.raises(MethodSpecError):
        spec = declaration()
        validate_method_spec(replace(spec, http_binding=replace(
            spec.http_binding, params_constraints=constraints,
        )))


def test_constraint_data_is_detached_frozen_and_exported_in_binding():
    raw = {"type": "object", "required": ["key"]}
    binding = HttpBinding(IdentityBinding("none"), params_constraints=raw)
    raw["required"].clear()
    spec = declaration(http_binding=binding)
    assert http_schema(spec)["required"] == ["key"]
    with pytest.raises(TypeError):
        binding.params_constraints["required"] = ()
    entry = export_catalog((spec,))["methods"][0]
    assert entry["httpBinding"]["paramsConstraints"] == {
        "type": "object", "required": ["key"],
    }
    baseline = deepcopy(entry)
    entry["httpBinding"]["paramsConstraints"]["required"].clear()
    assert export_catalog((spec,))["methods"][0] == baseline
