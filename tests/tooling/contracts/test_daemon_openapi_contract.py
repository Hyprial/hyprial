"""A-03: deterministic OpenAPI/catalog projection and its negative controls."""

from __future__ import annotations

import copy
import json

import pytest
from pathlib import Path

from hyprial.daemon import daemon_catalog, daemon_openapi
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import export_openapi

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "contract" / "daemon-request" / "openapi.json"


def _dump(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def test_the_committed_snapshot_is_the_deterministic_export() -> None:
    assert SNAPSHOT.is_file(), "run scripts/verify/dump_daemon_openapi.py --write"
    assert json.loads(SNAPSHOT.read_text(encoding="utf-8")) == daemon_openapi()
    assert _dump(daemon_openapi()) == _dump(daemon_openapi())


def test_export_is_independent_of_declaration_order() -> None:
    forward = export_openapi(METHOD_REGISTRY.specs)
    reverse = export_openapi(tuple(reversed(METHOD_REGISTRY.specs)))
    assert _dump(forward) == _dump(reverse)


def test_isolated_onboarding_export_declares_thirteen_paths_with_phases_and_examples() -> None:
    """A1 fixes the isolated contract; generating the snapshot is the A1b mechanical commit."""

    from hyprial.daemon.impl.request_catalog.onboarding import (
        ONBOARDING_EXAMPLES,
        ONBOARDING_SPECS,
    )

    document = export_openapi(ONBOARDING_SPECS, example_sets=ONBOARDING_EXAMPLES)
    expected = {f"/daemon/request/{spec.name}" for spec in ONBOARDING_SPECS}
    assert set(document["paths"]) == expected
    assert len(document["paths"]) == 13
    for operation in document["paths"].values():
        assert operation["post"]["x-hyprial-phase-caller"]
        assert operation["post"]["x-hyprial-examples"]
    # The isolated declaration must agree with how the registered production
    # registry projects these same thirteen methods.
    production = daemon_openapi()
    assert expected <= set(production["paths"])
    isolated_methods = {
        entry["name"]: entry for entry in document["x-hyprial-methods"]
    }
    production_methods = {
        entry["name"]: entry for entry in production["x-hyprial-methods"]
    }
    for name in isolated_methods:
        assert isolated_methods[name]["phaseCaller"] == production_methods[name]["phaseCaller"]
        assert isolated_methods[name]["errors"] == production_methods[name]["errors"]
    assert _dump(export_openapi(ONBOARDING_SPECS)) == _dump(export_openapi(ONBOARDING_SPECS))


def test_paths_are_the_explicit_http_projection_only() -> None:
    document = daemon_openapi()
    assert document["openapi"] == "3.1.0"
    assert document["info"] == {
        "title": "Hyprial daemon request contract",
        "version": "1",
    }
    # Exact equality made every new http_exposed MethodSpec edit this test;
    # the invariant is that the listed routes are present, while the rest of
    # the document (x-hyprial-methods) remains the full declaration catalog.
    assert {
        "/daemon/request/agent.create",
        "/daemon/request/agent.home-census",
        "/daemon/request/agent.list",
        "/daemon/request/agent.provider.default",
        "/daemon/request/agent.provider.login",
        "/daemon/request/agent.provider.logout",
        "/daemon/request/agent.secret.list",
        "/daemon/request/agent.start",
        "/daemon/request/agent.stop",
        "/daemon/request/routine.list",
        "/daemon/request/routine.status",
        "/daemon/request/workflow.list",
        "/daemon/request/org.create",
        "/daemon/request/org.fetch",
        "/daemon/request/org.invite",
        "/daemon/request/org.join",
        "/daemon/request/org.list",
        "/daemon/request/org.invites.list",
        "/daemon/request/orgfs.spaces",
        "/daemon/request/orgfs.read",
        "/daemon/request/orgfs.status",
        "/daemon/request/conversations.history",
        "/daemon/request/conversations.list",
    } <= set(document["paths"])
    methods = {entry["name"]: entry for entry in document["x-hyprial-methods"]}
    assert set(methods) == set(METHOD_REGISTRY.names)
    assert "daemon.catalog" in methods
    assert methods["daemon.catalog"]["httpExposed"] is False
    for retired in (
        "link.add", "link.drop", "link.list", "link.stats", "threads.get", "threads.list",
    ):
        assert retired not in methods
        assert f"/daemon/request/{retired}" not in document["paths"]
    # ACL stays declared for IPC/CLI but is deliberately absent from HTTP.
    for name in ("org.acl.grant", "org.acl.ls", "org.acl.revoke"):
        assert methods[name]["httpExposed"] is False
        assert methods[name]["httpBinding"] is None
        assert f"/daemon/request/{name}" not in document["paths"]
    assert methods["message.query"]["httpBinding"] == {
        "identity": {"mode": "local-owner-user-selector", "param": "actor"},
        "readiness": "inbox-service",
    }
    assert document["paths"]["/daemon/request/message.query"]["post"][
        "x-hyprial-http-binding"
    ]["identity"]["mode"] == "local-owner-user-selector"
    assert methods["conversations.list"]["idempotency"]["mode"] == "safe"
    assert methods["org.list"]["mutates"] is False
    assert methods["org.list"]["retry"] == "safe"
    assert methods["org.list"]["httpBinding"] == {
        "identity": {"mode": "none", "param": None},
        "readiness": None,
    }
    assert methods["org.fetch"]["mutates"] is True
    assert methods["org.fetch"]["retry"] == "never"
    assert methods["org.fetch"]["confirmation"] == "none"
    for name in ("org.create", "org.invite", "org.join"):
        assert methods[name]["mutates"] is True
        assert methods[name]["retry"] == "never"
        assert methods[name]["confirmation"] == "none"
        assert methods[name]["httpBinding"] == {
            "identity": {"mode": "none", "param": None},
            "readiness": None,
        }
    assert methods["org.fetch"]["httpBinding"] == {
        "identity": {"mode": "none", "param": None},
        "readiness": None,
    }
    assert methods["agent.list"]["mutates"] is False
    assert methods["agent.list"]["retry"] == "safe"
    assert methods["agent.list"]["httpExposed"] is True
    assert methods["agent.secret.list"]["mutates"] is False
    assert methods["agent.secret.list"]["httpExposed"] is True


def test_the_five_agent_methods_carry_same_source_examples() -> None:
    document = daemon_openapi()
    for method in (
        "agent.list",
        "agent.secret.list",
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
    ):
        operation = next(entry for entry in document["x-hyprial-methods"] if entry["name"] == method)
        cases = operation["x-hyprial-examples"]
        assert cases, method
        assert {case["kind"] for case in cases} & {"success"}, method
        assert {case["kind"] for case in cases} & {"invalid-params"}, method
        assert {case["kind"] for case in cases} & {
            "domain-error",
            "semantic-param-error",
        } or operation.get("x-hyprial-example-gaps", {}).get("domainErrors"), method


def test_http_request_bodies_carry_media_type_examples() -> None:
    document = daemon_openapi()
    fetch_body = document["paths"]["/daemon/request/org.fetch"]["post"]["requestBody"][
        "content"
    ]["application/json"]
    examples = fetch_body["examples"]
    assert {"mesh-with-candidates", "orgfs-refreshed"} <= set(examples)
    assert examples["mesh-with-candidates"]["value"] == {
        "requestMode": "neighbors",
        "timeoutSeconds": 2,
    }
    assert examples["orgfs-refreshed"]["value"] == {
        "from": "node-2",
        "timeoutSeconds": 2,
    }

    # org.list has an empty body, but its declared cases are still selectable
    # in Swagger instead of the request body showing no example at all.
    list_body = document["paths"]["/daemon/request/org.list"]["post"]["requestBody"][
        "content"
    ]["application/json"]
    assert list_body["examples"]
    assert all(example["value"] == {} for example in list_body["examples"].values())


def test_the_catalog_declares_complete_error_data_schemas() -> None:
    methods = {
        entry["name"]: entry for entry in daemon_catalog()["methods"]
    }
    for entry in methods.values():
        assert entry["paramsSchema"], entry["name"]
        assert entry["resultSchema"], entry["name"]
        assert entry["errors"], entry["name"]
        for error in entry["errors"]:
            assert error["code"]
            if error["dataSchema"] is not None:
                assert error["dataSchema"]["type"] == "object"
    invalid = next(
        error
        for error in methods["message.send"]["errors"]
        if error["code"] == "INVALID_ARGUMENT"
    )
    assert invalid["dataSchema"]["properties"]["target"]["type"] == "string"
    error_schema = daemon_openapi()["paths"]["/daemon/request/conversations.list"]["post"][
        "responses"
    ]["default"]["content"]["application/json"]["schema"]
    codes = {
        variant["properties"]["code"]["const"] for variant in error_schema["oneOf"]
    }
    assert {
        "INVALID_ARGUMENT",
        "SENDER_UNRESOLVED",
        "AGENT_ADDRESS_RETIRED",
        "SENDER_NOT_AUTHENTICATED",
        "AMBIGUOUS_TARGET",
        "STALE_SESSION",
        "SESSION_SUPERSEDED",
        "INVALID_SESSION_SOURCE",
        "DAEMON_NOT_READY",
    } <= codes


def _snapshot_results(document: dict) -> list[dict]:
    """Only the declared top.snapshot and top.inspect result copies carry synthetic runtime fields."""
    operation = document["paths"]["/daemon/request/top.snapshot"]["post"]
    method = next(
        entry
        for entry in document["x-hyprial-methods"]
        if entry["name"] == "top.snapshot"
    )
    results = list(
        operation["responses"]["200"]["content"]["application/json"]["schema"][
            "examples"
        ]
    )
    assert len(results) == 2
    for entry in (operation, method):
        cases = {case["name"]: case for case in entry["x-hyprial-examples"]}
        results.extend(cases[name]["result"] for name in ("empty-runtime", "no-actors"))
    inspect = next(
        entry
        for entry in document["x-hyprial-methods"]
        if entry["name"] == "top.inspect"
    )
    inspect_cases = {case["name"]: case for case in inspect["x-hyprial-examples"]}
    results.extend(
        inspect_cases[name]["result"]
        for name in ("one-actor", "no-process-sample", "no-actors")
    )
    return results


def _assert_no_runtime_metadata(document: dict) -> None:
    # Validate the fixed example values before removing only those exact fields.
    # Other example payloads and all metadata remain subject to the original guard.
    document = copy.deepcopy(document)
    for result in _snapshot_results(document):
        assert result.pop("nowMs") == 1762075800000
        assert result["daemon"].pop("pid") == 4242
        assert result["daemon"].pop("epoch") == "epoch-example"
        for row in result.get("actors", []):
            if "pid" in row:
                assert row["pid"] in (None, 123)
                row.pop("pid")
    pid_schema_paths = {
        "adapter.list": ("properties", "adapters", "items", "properties"),
        "adapter.start": ("properties", "adapter", "properties"),
        "adapter.status": ("properties", "adapter", "properties"),
        "adapter.stop": ("properties", "adapter", "oneOf", 0, "properties"),
    }
    methods = {entry["name"]: entry for entry in document["x-hyprial-methods"]}
    for name, path in pid_schema_paths.items():
        schemas = [
            methods[name]["resultSchema"],
            document["paths"][f"/daemon/request/{name}"]["post"]["responses"]["200"]["content"]["application/json"]["schema"],
        ]
        for schema in schemas:
            for segment in path:
                schema = schema[segment]
            assert schema.pop("pid") == {"minimum": 0, "type": "integer"}
    for case in methods["top.inspect"]["x-hyprial-examples"]:
        if "pid" in case.get("params", {}):
            assert case["params"]["pid"] == 123
            case["params"].pop("pid")
    text = json.dumps(document, sort_keys=True)
    for forbidden in ('"nowMs"', '"pid"', "HYPRIAL_HOME", "/Users/"):
        if forbidden in text:
            raise AssertionError(f"runtime metadata outside fixed fixtures: {forbidden}")
    assert set(document) == {
        "openapi",
        "jsonSchemaDialect",
        "info",
        "paths",
        "components",
        "x-hyprial-methods",
    }
    for entry in document["x-hyprial-methods"]:
        assert not {"epoch", "pid", "nowMs", "home", "socket"} & set(entry)
    for path in document["paths"].values():
        for operation in path.values():
            assert not {"epoch", "pid", "nowMs", "home", "socket"} & set(operation)
    if '"epoch": "' in text:
        raise AssertionError("runtime epoch outside fixed fixtures")


def test_the_projection_has_no_runtime_identity_or_clock_fields() -> None:
    _assert_no_runtime_metadata(json.loads(SNAPSHOT.read_text(encoding="utf-8")))


@pytest.mark.parametrize("copy_index", range(9))
@pytest.mark.parametrize(
    "field,value", [("nowMs", 1762075800001), ("pid", 4243), ("epoch", "live-epoch")]
)
def test_projection_rejects_nonfixture_runtime_values(
    copy_index: int, field: str, value: object
) -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    result = _snapshot_results(document)[copy_index]
    target = result if field == "nowMs" else result["daemon"]
    target[field] = value
    with pytest.raises(AssertionError):
        _assert_no_runtime_metadata(document)


@pytest.mark.parametrize("location", ["metadata", "other-example", "snapshot-extra"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("nowMs", 1762075800000),
        ("pid", 4242),
        ("epoch", "epoch-example"),
        ("path", "/Users/private"),
        ("config", "HYPRIAL_HOME"),
    ],
)
def test_projection_rejects_runtime_fields_outside_exact_fixture_locations(
    location: str, field: str, value: object
) -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if location == "metadata":
        target = document["info"]
    elif location == "other-example":
        target = document["paths"]["/daemon/request/org.list"]["post"]["requestBody"][
            "content"
        ]["application/json"]
        target["examples"]["runtime-leak"] = {"value": {field: value}}
        target = target["examples"]["runtime-leak"]["value"]
    else:
        result = _snapshot_results(document)[0]
        result["runtime-leak"] = {}
        target = result["runtime-leak"]
    target[field] = value
    with pytest.raises(AssertionError):
        _assert_no_runtime_metadata(document)


def test_projection_rejects_pid_in_example_properties() -> None:
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    operation = document["paths"]["/daemon/request/org.list"]["post"]
    examples = operation["requestBody"]["content"]["application/json"]["examples"]
    examples["runtime-leak"] = {"value": {"properties": {"pid": 4242}}}
    with pytest.raises(AssertionError):
        _assert_no_runtime_metadata(document)


