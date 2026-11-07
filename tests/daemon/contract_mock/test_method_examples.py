"""Generic same-schema example validation and OpenAPI projection."""

from __future__ import annotations

import json

import pytest

from hyprial.kernel import (
    MethodError,
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    MethodSpec,
    export_openapi,
    validate_method_example_set,
    validate_method_example_sets,
)

from .conftest import demo_example_sets, demo_specs


def _listed_set(example_sets: tuple[MethodExampleSet, ...]) -> MethodExampleSet:
    return example_sets[0]


def _write_set(example_sets: tuple[MethodExampleSet, ...]) -> MethodExampleSet:
    return example_sets[1]


def _operation(document: dict, method: str) -> dict:
    return document["paths"][f"/daemon/request/{method}"]["post"]


def test_valid_sets_project_same_schema_examples_and_gaps() -> None:
    document = export_openapi(demo_specs(), example_sets=demo_example_sets())
    listed = _operation(document, "demo.list")
    written = _operation(document, "demo.write")

    assert [case["kind"] for case in listed["x-hyprial-examples"]] == [
        "success",
        "empty",
        "invalid-params",
        "invalid-params",
        "domain-error",
        "semantic-param-error",
    ]
    assert listed["requestBody"]["content"]["application/json"]["schema"][
        "examples"
    ] == [
        {"filter": None},
        {"filter": "missing"},
        {"filter": None},
        {"filter": "unknown"},
    ]
    assert listed["responses"]["200"]["content"]["application/json"]["schema"][
        "examples"
    ] == [{"ok": True, "items": ["alpha"]}, {"ok": True, "items": []}]
    assert written["x-hyprial-example-gaps"] == {
        "empty": (
            "the write either returns its removal receipt or fails; it has no "
            "legal empty result"
        )
    }


def test_export_is_deterministic_and_declaration_order_independent() -> None:
    forward = export_openapi(demo_specs(), example_sets=demo_example_sets())
    reverse = export_openapi(
        tuple(reversed(demo_specs())), example_sets=tuple(reversed(demo_example_sets()))
    )
    assert json.dumps(forward, sort_keys=True) == json.dumps(reverse, sort_keys=True)
    assert json.dumps(forward, sort_keys=True) == json.dumps(
        export_openapi(demo_specs(), example_sets=demo_example_sets()), sort_keys=True
    )


def test_projection_does_not_mutate_declaration_schemas() -> None:
    specs = demo_specs()
    export_openapi(specs, example_sets=demo_example_sets())
    assert specs[0].params_schema == demo_specs()[0].params_schema
    assert "examples" not in specs[0].params_schema


def test_a_result_outside_the_declared_schema_is_rejected() -> None:
    example_sets = demo_example_sets()
    bad = MethodExampleSet(
        method="demo.list",
        cases=(
            MethodExample(
                name="bad-result",
                kind="success",
                description="the item type is wrong",
                params={"filter": None},
                result={"ok": True, "items": [1]},
            ),
        ),
    )
    with pytest.raises(Exception, match="result"):
        validate_method_example_set(demo_specs()[0], bad)
    assert example_sets[0].cases


def test_invalid_params_must_fail_the_params_schema() -> None:
    bad = MethodExampleSet(
        method="demo.list",
        cases=(
            MethodExample(
                name="not-invalid",
                kind="invalid-params",
                description="this body actually validates",
                params={"filter": None},
                error=MethodExampleError("INVALID_ARGUMENT", "nope"),
            ),
        ),
    )
    with pytest.raises(Exception, match="invalid|params"):
        validate_method_example_set(demo_specs()[0], bad)


def test_domain_error_requires_a_declared_code_and_valid_data() -> None:
    undeclared = MethodExampleSet(
        method="demo.list",
        cases=(
            MethodExample(
                name="undeclared",
                kind="domain-error",
                description="the code is not in the declaration",
                params={"filter": None},
                error=MethodExampleError("UNKNOWN_CODE", "nope"),
            ),
        ),
    )
    with pytest.raises(Exception, match="UNKNOWN_CODE|declared"):
        validate_method_example_set(demo_specs()[0], undeclared)

    bad_data = MethodExampleSet(
        method="demo.list",
        cases=(
            MethodExample(
                name="bad-data",
                kind="domain-error",
                description="the declared data schema is not satisfied",
                params={"filter": None},
                error=MethodExampleError("DAEMON_NOT_READY", "restoring", {"why": "x"}),
            ),
        ),
    )
    with pytest.raises(Exception, match="data"):
        validate_method_example_set(demo_specs()[0], bad_data)


def test_empty_and_unconfigured_require_a_reason() -> None:
    bad = MethodExampleSet(
        method="demo.list",
        cases=(
            MethodExample(
                name="empty-without-reason",
                kind="empty",
                description="the reason is missing",
                params={"filter": None},
                result={"ok": True, "items": []},
            ),
        ),
    )
    with pytest.raises(Exception, match="reason"):
        validate_method_example_set(demo_specs()[0], bad)


def test_missing_empty_and_domain_coverage_needs_an_explicit_exemption() -> None:
    only_success = MethodExampleSet(
        method="demo.write",
        cases=(
            MethodExample(
                name="removed",
                kind="success",
                description="the write succeeded",
                params={"actor": "agent-a", "setupToken": "example-not-a-credential"},
                result={"ok": True, "actor": "agent-a", "removed": []},
            ),
        ),
    )
    with pytest.raises(Exception, match="empty"):
        validate_method_example_set(demo_specs()[1], only_success)

    exempt = MethodExampleSet(
        method="demo.write",
        empty_not_applicable="no legal empty result",
        domain_errors_not_applicable="no domain error beyond parameter validation",
        cases=(
            MethodExample(
                name="removed",
                kind="success",
                description="the write succeeded",
                params={"actor": "agent-a", "setupToken": "example-not-a-credential"},
                result={"ok": True, "actor": "agent-a", "removed": []},
            ),
        ),
    )
    with pytest.raises(Exception, match="invalid-params"):
        validate_method_example_set(demo_specs()[1], exempt)


def test_duplicate_case_names_are_rejected() -> None:
    duplicate = MethodExampleSet(
        method="demo.list",
        cases=(
            MethodExample(
                name="same",
                kind="success",
                description="one",
                params={"filter": None},
                result={"ok": True, "items": []},
            ),
            MethodExample(
                name="same",
                kind="success",
                description="two",
                params={"filter": None},
                result={"ok": True, "items": []},
            ),
        ),
    )
    with pytest.raises(Exception, match="duplicate"):
        validate_method_example_set(demo_specs()[0], duplicate)


def test_unknown_or_non_exposed_method_sets_are_rejected() -> None:
    unknown = MethodExampleSet(
        method="demo.unknown",
        cases=(
            MethodExample(
                name="x",
                kind="success",
                description="x",
                params={},
                result={"ok": True},
            ),
        ),
    )
    with pytest.raises(Exception, match="unknown|declared"):
        validate_method_example_sets(demo_specs(), (unknown,))

    hidden_spec = MethodSpec(
        name="demo.hidden",
        owner="daemon.demo",
        params_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        errors=(MethodError("INVALID_ARGUMENT"),),
        http_exposed=False,
    )
    hidden_set = MethodExampleSet(
        method="demo.hidden",
        empty_not_applicable="not applicable",
        domain_errors_not_applicable="not applicable",
        cases=(
            MethodExample(
                name="ok",
                kind="success",
                description="ok",
                params={},
                result={"ok": True},
            ),
        ),
    )
    with pytest.raises(Exception, match="invalid-params"):
        validate_method_example_sets((hidden_spec,), (hidden_set,))


def _coverage_cases(error_kind: str, error_code: str) -> tuple[MethodExample, ...]:
    return (
        MethodExample(
            name="ok",
            kind="success",
            description="a normal result",
            params={"filter": None},
            result={"ok": True, "items": ["alpha"]},
        ),
        MethodExample(
            name="empty",
            kind="empty",
            description="a legal empty result",
            params={"filter": "missing"},
            result={"ok": True, "items": []},
            reason="an empty match is a legal success",
        ),
        MethodExample(
            name="schema-invalid",
            kind="invalid-params",
            description="the true pre-dispatch negative control",
            params={"filter": 5},
            error=MethodExampleError("INVALID_ARGUMENT", "wrong filter type"),
        ),
        MethodExample(
            name="handler-rejection",
            kind=error_kind,
            description="a schema-valid rejection from the handler or domain",
            params={"filter": "semantic"},
            error=MethodExampleError(error_code, "rejected after validation"),
        ),
    )


def test_domain_error_may_use_a_parameter_code() -> None:
    example_set = MethodExampleSet(
        method="demo.list",
        cases=_coverage_cases("domain-error", "INVALID_ARGUMENT"),
    )
    validate_method_example_set(demo_specs()[0], example_set)


def test_semantic_param_error_requires_schema_valid_params() -> None:
    good = MethodExampleSet(
        method="demo.list",
        cases=_coverage_cases("semantic-param-error", "INVALID_ARGUMENT"),
    )
    validate_method_example_set(demo_specs()[0], good)

    bad = MethodExampleSet(
        method="demo.list",
        cases=(
            MethodExample(
                name="not-semantic",
                kind="semantic-param-error",
                description="this is a schema-invalid body",
                params={"filter": 5},
                error=MethodExampleError("INVALID_ARGUMENT", "wrong filter type"),
            ),
        ),
    )
    with pytest.raises(Exception, match="params must pass|schema"):
        validate_method_example_set(demo_specs()[0], bad)


def _universal_object_spec() -> MethodSpec:
    value = {"type": ["string", "number", "boolean", "null", "array", "object"]}
    return MethodSpec(
        name="demo.universal",
        owner="daemon.demo",
        params_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": value,
        },
        result_schema={
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        errors=(MethodError("INVALID_ARGUMENT"),),
        http_exposed=True,
    )


def test_universal_object_schema_can_use_a_non_object_true_negative() -> None:
    universal = _universal_object_spec()
    example_set = MethodExampleSet(
        method=universal.name,
        empty_not_applicable="the method always returns its success object",
        cases=(
            MethodExample(
                name="ok",
                kind="success",
                description="an empty object is accepted",
                params={},
                result={"ok": True},
            ),
            MethodExample(
                name="non-object",
                kind="invalid-params",
                description="only a non-object is structurally invalid",
                params=[],
                error=MethodExampleError("INVALID_ARGUMENT", "params must be an object"),
            ),
            MethodExample(
                name="semantic",
                kind="semantic-param-error",
                description="the handler may reject an otherwise valid object",
                params={},
                error=MethodExampleError("INVALID_ARGUMENT", "handler rejected the object"),
            ),
        ),
    )
    validate_method_example_set(universal, example_set)


def test_non_invalid_cases_still_require_object_params() -> None:
    universal = _universal_object_spec()
    example_set = MethodExampleSet(
        method=universal.name,
        empty_not_applicable="not applicable",
        domain_errors_not_applicable="not applicable",
        cases=(
            MethodExample(
                name="bad-success",
                kind="success",
                description="a success example cannot be a list",
                params=[],
                result={"ok": True},
            ),
            MethodExample(
                name="non-object",
                kind="invalid-params",
                description="the true negative",
                params=[],
                error=MethodExampleError("INVALID_ARGUMENT", "params must be an object"),
            ),
        ),
    )
    with pytest.raises(Exception, match="params must be a JSON object"):
        validate_method_example_set(universal, example_set)
