"""Contract and schema tests for the registered agent.cell.* MethodSpecs (I6-4)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.agent import PARTIAL_MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.agent.cell import (
    AGENT_CELL_GET,
    AGENT_CELL_GET_SPEC,
    AGENT_CELL_UPDATE,
    AGENT_CELL_UPDATE_SPEC,
    CELL_BINDINGS,
    CELL_EXAMPLES,
    CELL_SPECS,
    cell_update,
)
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import export_openapi, ipc_errors, validate_method_specs

ROOT = Path(__file__).resolve().parents[6]


def test_agent_cell_methods_are_registered_in_i6_4() -> None:
    assert AGENT_CELL_GET in METHOD_REGISTRY
    assert AGENT_CELL_UPDATE in METHOD_REGISTRY
    assert AGENT_CELL_GET in PARTIAL_MIGRATED_METHODS
    assert AGENT_CELL_UPDATE in PARTIAL_MIGRATED_METHODS

    openapi = json.loads((ROOT / "contract/daemon-request/openapi.json").read_text(encoding="utf-8"))
    exported_names = {entry["name"] for entry in openapi["x-hyprial-methods"]}
    assert AGENT_CELL_GET in exported_names
    assert AGENT_CELL_UPDATE in exported_names


def test_specs_declare_http_exposed_retry_modes_and_strict_schemas() -> None:
    validate_method_specs(CELL_SPECS, metaschema=True)

    assert AGENT_CELL_GET_SPEC.http_exposed is True
    assert AGENT_CELL_GET_SPEC.mutates is False
    assert AGENT_CELL_GET_SPEC.retry == "safe"
    assert AGENT_CELL_GET_SPEC.idempotency is not None
    assert AGENT_CELL_GET_SPEC.idempotency.mode == "safe"
    assert AGENT_CELL_GET_SPEC.params_schema["additionalProperties"] is False
    assert AGENT_CELL_GET_SPEC.result_schema["additionalProperties"] is False
    assert ipc_errors.USER_AGENT_IMMUTABLE in {err.code for err in AGENT_CELL_GET_SPEC.errors}

    assert AGENT_CELL_UPDATE_SPEC.http_exposed is True
    assert AGENT_CELL_UPDATE_SPEC.mutates is True
    assert AGENT_CELL_UPDATE_SPEC.retry == "never"
    assert AGENT_CELL_UPDATE_SPEC.idempotency is not None
    assert AGENT_CELL_UPDATE_SPEC.idempotency.mode == "never"
    assert AGENT_CELL_UPDATE_SPEC.audience == ("local-operator",)
    assert "expectedRevision" in AGENT_CELL_UPDATE_SPEC.params_schema["required"]
    assert AGENT_CELL_UPDATE_SPEC.params_schema["additionalProperties"] is False
    assert "propertyNames" not in AGENT_CELL_UPDATE_SPEC.params_schema["properties"]["params"]
    assert AGENT_CELL_UPDATE_SPEC.result_schema["additionalProperties"] is False

    invalid_error = next(
        err for err in AGENT_CELL_UPDATE_SPEC.errors if err.code == ipc_errors.INVALID_ARGUMENT
    )
    assert invalid_error.data_schema is not None
    assert set(invalid_error.data_schema["properties"]) == {"expected", "current"}


def test_isolated_openapi_export_validates_specs_and_canonical_examples() -> None:
    document = export_openapi(CELL_SPECS, example_sets=CELL_EXAMPLES)
    assert set(document["paths"]) == {
        f"/daemon/request/{AGENT_CELL_GET}",
        f"/daemon/request/{AGENT_CELL_UPDATE}",
    }
    for path in document["paths"].values():
        assert path["post"]["x-hyprial-examples"]


def test_isolated_openapi_projects_cleanly_through_contract_mock() -> None:
    node = shutil.which("node")
    assert node is not None
    document = export_openapi(CELL_SPECS, example_sets=CELL_EXAMPLES)
    script = (
        'import fs from "node:fs";'
        'import { projectContract } from "./gui/contract-mock/contract.ts";'
        'const openapi = JSON.parse(fs.readFileSync(0, "utf8"));'
        'const envelope = JSON.parse(fs.readFileSync("contract/daemon-request/http-envelope.schema.json", "utf8"));'
        "const projected = projectContract(openapi, envelope);"
        "console.log(JSON.stringify(Object.keys(projected.methods)));"
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=json.dumps(document),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [AGENT_CELL_GET, AGENT_CELL_UPDATE]


def test_merged_openapi_export_passes_runtime_metadata_guard() -> None:
    from tests.tooling.contracts.test_daemon_openapi_contract import (
        _assert_no_runtime_metadata,
    )

    base = json.loads(
        (ROOT / "contract/daemon-request/openapi.json").read_text(encoding="utf-8")
    )
    isolated = export_openapi(CELL_SPECS, example_sets=CELL_EXAMPLES)
    merged = {
        **base,
        "paths": {**base["paths"], **isolated["paths"]},
        "x-hyprial-methods": [*base["x-hyprial-methods"], *isolated["x-hyprial-methods"]],
    }
    _assert_no_runtime_metadata(merged)

    get_examples = isolated["paths"][f"/daemon/request/{AGENT_CELL_GET}"]["post"][
        "x-hyprial-examples"
    ]
    active_case = next(case for case in get_examples if case["name"] == "active-cell")
    assert active_case["result"]["snapshot"] is None


def test_isolated_registry_invokes_handlers_and_validates_cas_data() -> None:
    from tests.daemon.application.cell_admin.test_service import (
        AGENT_URI,
        CELL_ID,
        _make_app,
        _make_cell,
        _make_snapshot,
    )
    from hyprial.kernel import DaemonRequestError

    registry = MethodRegistry.from_bindings(CELL_BINDINGS)
    registry.verify()
    update_binding = registry.get(AGENT_CELL_UPDATE)
    assert update_binding is not None
    assert update_binding.preauthorize is not None

    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot(revision=7, effective_revision=7))

    fetched = registry.invoke(app, AGENT_CELL_GET, {"name": "worker"})
    assert fetched["ok"] is True
    assert fetched["cell"]["cellId"] == CELL_ID
    assert fetched["cell"]["revision"] == 7

    with pytest.raises(DaemonRequestError) as conflict:
        registry.invoke(
            app,
            AGENT_CELL_UPDATE,
            {
                "name": "worker",
                "expectedRevision": 6,
                "members": [{"ref": AGENT_URI, "role": "harness"}],
            },
        )
    assert conflict.value.code == ipc_errors.INVALID_ARGUMENT
    assert conflict.value.data == {"expected": 6, "current": 7}

    for target in ("worker", "hyprial-user"):
        with pytest.raises(DaemonRequestError) as unauthorized:
            registry.invoke(
                app,
                AGENT_CELL_UPDATE,
                {
                    "name": target,
                    "expectedRevision": 7,
                    "members": [{"ref": AGENT_URI, "role": "harness"}],
                },
                trusted_message_origin="worker",
            )
        assert unauthorized.value.code == ipc_errors.CALLER_NOT_AUTHORIZED

    with pytest.raises(DaemonRequestError) as user_update_refused:
        registry.invoke(
            app,
            AGENT_CELL_UPDATE,
            {
                "name": "hyprial-user",
                "expectedRevision": 7,
                "cellName": "x",
            },
            trusted_message_origin="ipc",
        )
    assert user_update_refused.value.code == ipc_errors.DAEMON_ERROR

    with pytest.raises(DaemonRequestError) as direct_unauthorized:
        cell_update(
            app,
            {"name": "worker", "expectedRevision": 7, "cellName": "x"},
            trusted_message_origin="worker",
        )
    assert direct_unauthorized.value.code == ipc_errors.CALLER_NOT_AUTHORIZED

    for bad_params in ({"org.issuer": "evil"}, {"": "bad"}):
        with pytest.raises(DaemonRequestError) as param_refused:
            registry.invoke(
                app,
                AGENT_CELL_UPDATE,
                {
                    "name": "worker",
                    "expectedRevision": 7,
                    "params": bad_params,
                },
            )
        assert param_refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []

    with pytest.raises(DaemonRequestError) as user_get_refused:
        registry.invoke(app, AGENT_CELL_GET, {"name": "hyprial-user"})
    assert user_get_refused.value.code == ipc_errors.DAEMON_ERROR


def test_cell_catalog_import_does_not_load_application_composition_mcp_or_uvicorn() -> None:
    script = (
        "import sys; "
        "from hyprial.daemon.impl.request_catalog.agent.cell import CELL_EXAMPLES, CELL_SPECS; "
        "from hyprial.kernel import export_openapi; "
        "export_openapi(CELL_SPECS, example_sets=CELL_EXAMPLES); "
        "loaded = sorted(m for m in ('uvicorn', 'mcp', 'hyprial.daemon.impl.composition', "
        "'hyprial.daemon.impl.application') if m in sys.modules); "
        "assert not loaded, loaded"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
