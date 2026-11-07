"""A-03: deterministic OpenAPI/catalog projection and its negative controls."""

from __future__ import annotations

import json
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


def test_paths_are_the_explicit_http_projection_only() -> None:
    document = daemon_openapi()
    assert document["openapi"] == "3.1.0"
    assert document["info"] == {
        "title": "Hyprial daemon request contract",
        "version": "1",
    }
    assert set(document["paths"]) == {
        "/daemon/request/agent.list",
        "/daemon/request/agent.provider.default",
        "/daemon/request/agent.secret.list",
        "/daemon/request/link.list",
        "/daemon/request/link.stats",
    }
    methods = {entry["name"]: entry for entry in document["x-hyprial-methods"]}
    assert set(methods) == set(METHOD_REGISTRY.names)
    assert "daemon.catalog" in methods
    assert methods["daemon.catalog"]["httpExposed"] is False
    assert methods["link.list"]["httpBinding"] == {
        "identity": {"mode": "local-operator-selector", "param": "from"},
        "readiness": "link-service",
    }
    assert methods["link.add"]["httpBinding"] is None
    assert document["paths"]["/daemon/request/link.list"]["post"][
        "x-hyprial-http-binding"
    ]["identity"]["mode"] == "local-operator-selector"
    assert methods["link.add"]["mutates"] is True
    assert methods["link.add"]["httpExposed"] is False
    assert methods["link.drop"]["httpExposed"] is False
    assert methods["link.list"]["confirmation"] == "none"
    assert methods["link.list"]["idempotency"]["mode"] == "safe"
    assert methods["link.add"]["idempotency"]["mode"] == "never"
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
        for error in methods["link.add"]["errors"]
        if error["code"] == "INVALID_ARGUMENT"
    )
    assert invalid["dataSchema"]["properties"]["peer"]["type"] == "string"
    error_schema = daemon_openapi()["paths"]["/daemon/request/link.list"]["post"][
        "responses"
    ]["default"]["content"]["application/json"]["schema"]
    codes = {
        variant["properties"]["code"]["const"] for variant in error_schema["oneOf"]
    }
    assert codes == {"INVALID_ARGUMENT", "DAEMON_NOT_READY"}


def test_the_projection_has_no_runtime_identity_or_clock_fields() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    for forbidden in ('"nowMs"', '"pid"', "HYPRIAL_HOME", "/Users/"):
        assert forbidden not in text
    document = json.loads(text)
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
    assert '"epoch": "' not in text
