"""Missing data stays missing; present data must satisfy its declaration."""

from dataclasses import replace
import json

import pytest
from jsonschema import Draft202012Validator

from contract_mock import ContractError, check_contract, load_contract
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import DaemonRequestError, export_openapi, validate_method_example_set
from hyprial.kernel.impl.contracts.daemon.examples import ExampleSpecError, example_to_json

from .helpers import DATA_SCHEMA, declaration, examples


@pytest.mark.parametrize("data", [None, {}, {"field": "summary"}])
def test_optional_data_examples_registry_and_openapi_agree(data):
    spec, cases = declaration(), examples(data)
    validate_method_example_set(spec, cases)
    encoded = example_to_json(cases.cases[-1])["error"]
    assert ("data" in encoded) == (data is not None)
    if data is not None:
        assert encoded["data"] == data

    def refuse(app, params, **kwargs):
        raise DaemonRequestError("INVALID_ARGUMENT", "synthetic refusal", data)

    registry = MethodRegistry.from_bindings(((spec, refuse),))
    with pytest.raises(DaemonRequestError) as raised:
        registry.invoke(None, spec.name, {})
    assert raised.value.code == "INVALID_ARGUMENT"
    assert raised.value.data == data
    operation = export_openapi((spec,), example_sets=(cases,))["paths"][
        "/daemon/request/probe.error"]["post"]
    schema = operation["responses"]["default"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["code", "message"]
    Draft202012Validator(schema).validate(encoded)
    assert not Draft202012Validator(schema).is_valid({**encoded, "data": None})


@pytest.mark.parametrize("schema,data", [
    (None, {}), (None, {"field": "x"}), (DATA_SCHEMA, {"field": 7}),
    (DATA_SCHEMA, {"secret": "synthetic"}),
    ({**DATA_SCHEMA, "required": ["field"]}, {}),
])
def test_present_invalid_or_undeclared_data_is_rejected(schema, data):
    spec = declaration(schema)
    with pytest.raises(ExampleSpecError, match="data"):
        validate_method_example_set(spec, examples(data))

    def refuse(app, params, **kwargs):
        raise DaemonRequestError("INVALID_ARGUMENT", "synthetic refusal", data)

    registry = MethodRegistry.from_bindings(((spec, refuse),))
    with pytest.raises(DaemonRequestError) as raised:
        registry.invoke(None, spec.name, {})
    assert raised.value.code == "DAEMON_ERROR"


def test_no_schema_allows_omission_and_none_serializes_as_omission():
    cases = examples(None)
    validate_method_example_set(declaration(None), cases)
    explicit_none = replace(cases.cases[-1].error, data=None)
    assert example_to_json(replace(cases.cases[-1], error=explicit_none))["error"] == {
        "code": "INVALID_ARGUMENT", "message": "synthetic refusal",
    }


@pytest.mark.parametrize("data", [None, {}, {"field": "summary"}])
def test_mock_load_and_checks_preserve_missing_or_present_data(tmp_path, data):
    """Load a modified populated document independently of example validation."""
    document = export_openapi((declaration(),), example_sets=(examples({"field": "x"}),))
    operation = document["paths"]["/daemon/request/probe.error"]["post"]
    for case in operation["x-hyprial-examples"]:
        if "error" in case:
            if data is None:
                case["error"].pop("data")
            else:
                case["error"]["data"] = data
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(document))
    contract = load_contract(path)
    check_contract(contract)
    for case in contract.methods["probe.error"].examples:
        if "error" in case:
            assert ("data" in case["error"]) == (data is not None)
            if data is not None:
                assert case["error"]["data"] == data


@pytest.mark.parametrize("schema,data", [
    (None, {}), (None, None), (DATA_SCHEMA, None),
    (DATA_SCHEMA, {"field": 7}), (DATA_SCHEMA, {"unknown": "x"}),
])
def test_serialized_mock_errors_reject_explicit_null_invalid_and_undeclared_data(tmp_path, schema, data):
    seed = {"field": "x"} if schema else None
    document = export_openapi((declaration(schema),), example_sets=(examples(seed),))
    operation = document["paths"]["/daemon/request/probe.error"]["post"]
    operation["x-hyprial-examples"][-1]["error"]["data"] = data
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ContractError, match="data"):
        load_contract(path)


def test_optional_example_data_does_not_change_declaration_or_http_digest():
    from tests.shell.request_http.test_catalog import _view

    views = []
    for data in (None, {}, {"field": "summary"}):
        doc = export_openapi((declaration(),), example_sets=(examples(data),))
        rows = [{**row, "declared": True, "state": "available", "callable": True,
                 "reason": None} for row in doc["x-hyprial-methods"]]
        views.append(_view(rows))
    assert len({view.declaration_digest for view in views}) == 1
    assert len({view.http_contract_digest for view in views}) == 1
