"""Export and load every write example through the Contract Mock loader."""

from __future__ import annotations

import json

import pytest

from contract_mock import load_contract
from hyprial.daemon.impl.request_catalog.org.orgfs.write_examples import ORGFS_WRITE_EXAMPLES
from hyprial.daemon.impl.request_catalog.org.orgfs.write_specs import BATCH4_ORGFS_BINDINGS
from hyprial.kernel import export_openapi, validate_method_example_sets

from .producers import plain


def http_params(contract, method, case):
    return next(row["params"] for row in contract.methods[method].examples if row["name"] == case.name)


SPECS = tuple(spec for spec, _handler in BATCH4_ORGFS_BINDINGS)


@pytest.fixture(scope="module")
def write_contract(tmp_path_factory):
    validated = validate_method_example_sets(SPECS, ORGFS_WRITE_EXAMPLES)
    assert {item.method for item in validated} == {spec.name for spec in SPECS}
    assert len(validated) == 17
    document = export_openapi(SPECS, example_sets=validated)
    path = tmp_path_factory.mktemp("write-contract") / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_contract(path)


@pytest.mark.parametrize("method", ["orgfs.export", "orgfs.import"])
def test_http_projection_preserves_full_methodspec_validation(write_contract, method):
    from dataclasses import replace

    from hyprial.kernel import ExampleSpecError

    example_set = next(item for item in ORGFS_WRITE_EXAMPLES if item.method == method)
    for case in example_set.cases:
        if case.kind == "invalid-params":
            continue
        assert case.params["from"] == "user:example"
        projected = http_params(write_contract, method, case)
        assert "from" not in projected
        assert projected == {key: plain(value) for key, value in case.params.items() if key != "from"}
    first = example_set.cases[0]
    corrupted = replace(first, params={key: value for key, value in first.params.items() if key != "from"})
    broken_set = replace(example_set, cases=(corrupted, *example_set.cases[1:]))
    with pytest.raises(ExampleSpecError, match="params must pass"):
        export_openapi(SPECS, example_sets=(broken_set,))
