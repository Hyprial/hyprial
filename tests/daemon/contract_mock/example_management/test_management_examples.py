"""Validate management examples through the exporter and the Contract Mock loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract_mock.contract import load_contract
from hyprial.daemon.impl.request_catalog.org.management_examples import (
    ORG_MANAGEMENT_EXAMPLES,
)
from hyprial.daemon.impl.request_catalog.org.network_specs import (
    ORG_NETWORK_SPEC,
    ORG_PUBLISH_SPEC,
    ORG_REPAIR_SPEC,
)
from hyprial.daemon.impl.request_catalog.org.removal_specs import (
    ORG_DELETE_SPEC,
    ORG_EXECUTE_SPEC,
    ORG_LEAVE_SPEC,
    ORG_REMOVE_SPEC,
)
from hyprial.daemon.impl.request_catalog.org.view_specs import (
    HOSTS_SPEC,
    TOP_SNAPSHOT_SPEC,
)
from hyprial.kernel import export_openapi


SPECS = (
    HOSTS_SPEC,
    TOP_SNAPSHOT_SPEC,
    ORG_DELETE_SPEC,
    ORG_EXECUTE_SPEC,
    ORG_LEAVE_SPEC,
    ORG_NETWORK_SPEC,
    ORG_PUBLISH_SPEC,
    ORG_REMOVE_SPEC,
    ORG_REPAIR_SPEC,
)


def test_management_examples_export_and_have_all_cases() -> None:
    document = export_openapi(SPECS, example_sets=ORG_MANAGEMENT_EXAMPLES)
    operations = {
        method: document["paths"][f"/daemon/request/{method}"]["post"]
        for method in {spec.name for spec in SPECS}
    }
    assert set(operations) == {example.method for example in ORG_MANAGEMENT_EXAMPLES}
    assert all(operation["x-hyprial-examples"] for operation in operations.values())
    assert "success" not in operations["org.delete"].get("x-hyprial-example-gaps", {})
    assert any(
        case["kind"] == "success"
        for case in operations["org.delete"]["x-hyprial-examples"]
    )


def test_management_examples_are_consumed_by_contract_mock_loader(
    tmp_path: Path,
) -> None:
    document = export_openapi(SPECS, example_sets=ORG_MANAGEMENT_EXAMPLES)
    contract_path = tmp_path / "openapi.json"
    envelope_path = Path("contract/daemon-request/http-envelope.schema.json")
    contract_path.write_text(json.dumps(document), encoding="utf-8")
    loaded = load_contract(contract_path, envelope_path)
    assert loaded.case("hosts", "online-host")["result"]["hosts"]
    assert (
        loaded.case("org.delete", "already-deleted")["error"]["code"]
        == "ORG_ALREADY_DELETED"
    )
    assert loaded.methods["org.delete"].cases_for("success")


def test_success_gap_cannot_coexist_with_success_case() -> None:
    from hyprial.daemon.impl.request_catalog.org.management_examples.values import (
        delete_complete,
    )
    from hyprial.kernel import MethodExample, MethodExampleSet

    bad = MethodExampleSet(
        method="org.delete",
        success_not_applicable="not applicable",
        empty_not_applicable="not applicable",
        cases=(
            MethodExample(
                name="success",
                kind="success",
                description="contradictory",
                params={"org": "group-hyprial-dev"},
                result=delete_complete(),
            ),
        ),
    )
    with pytest.raises(Exception, match="success_not_applicable"):
        export_openapi((ORG_DELETE_SPEC,), example_sets=(bad,))


def test_missing_success_without_explicit_gap_still_fails():
    from dataclasses import replace
    from hyprial.kernel import ExampleSpecError

    source = next(group for group in ORG_MANAGEMENT_EXAMPLES if group.method == "hosts")
    incomplete = replace(
        source, cases=tuple(case for case in source.cases if case.kind != "success")
    )
    with pytest.raises(ExampleSpecError, match="at least one success"):
        export_openapi((HOSTS_SPEC,), example_sets=(incomplete,))


def test_full_params_validation_precedes_http_identity_projection():
    from dataclasses import replace
    from hyprial.daemon.impl.request_catalog.org.orgfs.write_examples import (
        ORGFS_WRITE_EXAMPLES,
    )
    from hyprial.daemon.impl.request_catalog.org.orgfs.write_specs import (
        ORGFS_EXPORT_SPEC,
    )
    from hyprial.kernel import ExampleSpecError

    source = next(
        group for group in ORGFS_WRITE_EXAMPLES if group.method == "orgfs.export"
    )
    document = export_openapi((ORGFS_EXPORT_SPEC,), example_sets=(source,))
    operation = document["paths"]["/daemon/request/orgfs.export"]["post"]
    assert all("from" not in case["params"] for case in operation["x-hyprial-examples"])
    invalid_source = replace(
        source,
        cases=tuple(
            replace(case, params={**case.params, "from": 123})
            if case.kind == "success"
            else case
            for case in source.cases
        ),
    )
    with pytest.raises(ExampleSpecError, match="params must pass"):
        export_openapi((ORGFS_EXPORT_SPEC,), example_sets=(invalid_source,))
