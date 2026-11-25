"""H-03 canonical HTTP OpenAPI projection, with synthetic read/write methods.

The fixtures are invented here on purpose: H-03 must not learn the five Agent
method names, and the projection must be driven only by an already-admitted
daemon catalog entry.  These tests establish the pure projector; they do not
claim the H-01 runtime is wired to it yet.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
from jsonschema import Draft202012Validator

from hyprial.shell.impl.http_contract import (
    BEARER_SCHEME,
    DISPATCH_STATES,
    HTTP_CONTRACT_VERSION,
    OPENAPI_VERSION,
    STATUS_DESCRIPTIONS,
    TRANSPORT_ERRORS,
    HttpMethod,
    ProjectionError,
    build_openapi,
    canonical_json,
    document_digest,
    owners_from_document,
    project_catalog,
)
from hyprial.shell.impl.http_contract.__main__ import main

PARAMS: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "agent": {"type": "string", "minLength": 1},
        "clear": {"type": "boolean"},
    },
    "required": ["agent"],
}
RESULT: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"agent": {"type": "string"}, "provider": {"type": ["string", "null"]}},
    "required": ["agent", "provider"],
}
ERRORS: list[dict[str, object]] = [
    {
        "code": "INVALID_ARGUMENT",
        "description": "the request does not match the declared schema",
        "dataSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"field": {"type": "string"}},
            "required": ["field"],
        },
    },
    {
        "code": "CALLER_NOT_AUTHORIZED",
        "description": "the daemon refused this caller for the target",
        "dataSchema": None,
    },
]


def _entry(name: str = "demo.read", **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "owner": "demo",
        "state": "available",
        "callable": True,
        "reason": None,
        "mode": "unary",
        "mutates": False,
        "retry": "safe",
        "confirmation": "none",
        "audience": ["local-operator"],
        "httpBinding": {
            "identity": {"mode": "none", "param": None},
            "readiness": None,
        },
        "paramsSchema": PARAMS,
        "resultSchema": RESULT,
        "errors": ERRORS,
    }
    entry.update(overrides)
    return entry


def _write_entry(name: str = "demo.write", **overrides: object) -> dict[str, object]:
    entry = _entry(name)
    entry.update(
        {
            "mutates": True,
            "retry": "never",
            "httpBinding": {
                "identity": {"mode": "local-owner-user-selector", "param": "agent"},
                "readiness": None,
            },
        }
    )
    entry.update(overrides)
    return entry


def _methods() -> tuple[HttpMethod, HttpMethod]:
    return (
        HttpMethod.from_catalog_entry(_entry()),
        HttpMethod.from_catalog_entry(_write_entry()),
    )


def test_write_is_projected_as_never_retried_and_reads_stay_safe() -> None:
    document = build_openapi(_methods())
    read = document["paths"]["/daemon/request/demo.read"]["post"]
    write = document["paths"]["/daemon/request/demo.write"]["post"]

    assert read["x-hyprial-call-profile"] == "read"
    assert read["x-hyprial-mutates"] is False
    assert read["x-hyprial-retry"] == "safe"
    assert write["x-hyprial-call-profile"] == "write"
    assert write["x-hyprial-mutates"] is True
    assert write["x-hyprial-retry"] == "never"
    assert write["x-hyprial-confirmation"] == "none"
    assert "never retried" in write["description"]
    assert "unknown" in write["description"]
    assert "retry is safe" in read["description"]


def test_every_operation_declares_server_bound_caller_and_no_inferred_target() -> None:
    document = build_openapi(_methods())
    read = document["paths"]["/daemon/request/demo.read"]["post"]
    write = document["paths"]["/daemon/request/demo.write"]["post"]

    for operation in (read, write):
        assert operation["security"] == [{BEARER_SCHEME: []}]
        assert operation["x-hyprial-caller"]["source"] == "server-binding"
        assert operation["x-hyprial-caller"]["authority"] == "transport"
        assert operation["x-hyprial-target"] is None
    assert "selector" not in read["x-hyprial-caller"]
    assert write["x-hyprial-caller"]["selector"] == {
        "param": "agent",
        "mode": "local-owner-user-selector",
        "missing": "injected-from-server",
        "foreign": "refused-before-dispatch",
    }
    params = document["components"]["schemas"]["params.demo.write"]
    assert params["required"] == ["agent"]
    assert params["properties"]["agent"] == {"type": "string", "minLength": 1}


def test_caller_selector_is_valid_on_a_read_and_is_never_a_business_target() -> None:
    entry = _entry(
        httpBinding={
            "identity": {"mode": "local-owner-user-selector", "param": "agent"},
            "readiness": None,
        }
    )
    document = build_openapi((HttpMethod.from_catalog_entry(entry),))
    operation = document["paths"]["/daemon/request/demo.read"]["post"]
    assert operation["x-hyprial-caller"]["selector"]["param"] == "agent"
    assert operation["x-hyprial-target"] is None
    description = operation["requestBody"]["content"]["application/json"]["description"]
    assert "not substituted" not in description
    assert "caller" in description
    assert "injected" in description


def test_owner_user_selector_projects_optional_actor_from_trusted_owner() -> None:
    entry = _entry(
        paramsSchema={
            "type": "object", "additionalProperties": False,
            "properties": {"actor": {"type": "string", "minLength": 1}},
        },
        httpBinding={
            "identity": {"mode": "local-owner-user-selector", "param": "actor"},
            "readiness": "inbox-service",
        },
    )
    document = build_openapi((HttpMethod.from_catalog_entry(entry),))
    operation = document["paths"]["/daemon/request/demo.read"]["post"]
    assert operation["x-hyprial-caller"]["selector"] == {
        "param": "actor", "mode": "local-owner-user-selector",
        "missing": "injected-from-server", "foreign": "refused-before-dispatch",
    }
    assert "actor" not in document["components"]["schemas"]["params.demo.read"].get("required", [])
    assert "trusted owner" in operation["description"]


def test_same_source_examples_become_named_swagger_request_and_response_values() -> None:
    cases = [
        {"name": "one", "kind": "success", "description": "one synthetic result",
         "params": {"agent": "example-agent"},
         "result": {"agent": "example-agent", "provider": None}},
        {"name": "missing-agent", "kind": "invalid-params", "description": "agent is required",
         "params": {}, "error": {"code": "INVALID_ARGUMENT", "message": "agent is required"}},
    ]
    document = project_catalog(
        {"httpContractVersion": HTTP_CONTRACT_VERSION, "methods": [_entry()]},
        examples={"demo.read": cases},
    )
    operation = document["paths"]["/daemon/request/demo.read"]["post"]
    request = operation["requestBody"]["content"]["application/json"]["examples"]
    success = operation["responses"]["200"]["content"]["application/json"]["examples"]
    assert request["one"]["value"] == {"agent": "example-agent"}
    assert request["missing-agent"]["value"] == {}
    assert success["one"]["value"]["result"] == {"agent": "example-agent", "provider": None}
    assert success["one"]["value"]["method"] == "demo.read"
    assert "missing-agent" not in success
def test_owner_selector_is_injected_and_explicit_identity_is_a_400() -> None:
    entry = _write_entry(
        httpBinding={
            "identity": {"mode": "local-owner-selector", "param": "agent"},
            "readiness": None,
        }
    )
    document = build_openapi((HttpMethod.from_catalog_entry(entry),))
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    selector = operation["x-hyprial-caller"]["selector"]
    assert selector["mode"] == "local-owner-selector"
    assert selector["foreign"] == "refused-with-400-before-dispatch"
    description = operation["requestBody"]["content"]["application/json"]["description"]
    assert "refused with 400 before dispatch" in description
    assert "403" not in description


def test_business_targets_are_ordinary_params_with_or_without_a_selector() -> None:
    entry = _write_entry(
        httpBinding={
            "identity": {"mode": "none", "param": None},
            "readiness": None,
        }
    )
    document = build_openapi((HttpMethod.from_catalog_entry(entry),))
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    assert operation["x-hyprial-caller"] == {
        "source": "server-binding",
        "authority": "transport",
    }
    assert operation["x-hyprial-target"] is None
    assert document["components"]["schemas"]["params.demo.write"]["required"] == [
        "agent"
    ]


def test_route_payload_without_owner_is_consumable_and_owner_adapter_is_explicit() -> None:
    entry = _entry()
    entry.pop("owner")
    document = project_catalog({"methods": [entry]})
    operation = document["paths"]["/daemon/request/demo.read"]["post"]
    assert "tags" not in operation
    assert document["tags"] == []
    assert document["x-hyprial-http-contract"]["methods"][0]["owner"] is None

    owners = owners_from_document(
        {"methods": [{"name": "demo.read", "owner": "declared.demo"}]}
    )
    assert owners == {"demo.read": "declared.demo"}
    assert owners_from_document({"demo.read": "plain.owner"}) == {
        "demo.read": "plain.owner"
    }
    with_owner = project_catalog({"methods": [entry]}, owners=owners)
    assert with_owner["paths"]["/daemon/request/demo.read"]["post"]["tags"] == [
        "declared.demo"
    ]
    assert with_owner["tags"] == [
        {"name": "declared.demo", "description": "Methods declared by declared.demo"}
    ]


def test_owner_adapter_never_guesses_or_invents_metadata() -> None:
    entry = _entry()
    entry.pop("owner")

    # Missing authoritative source: no owner is guessed and no tag is invented.
    no_source = project_catalog({"methods": [entry]})
    assert "tags" not in no_source["paths"]["/daemon/request/demo.read"]["post"]
    assert no_source["tags"] == []

    # A map entry for another method neither attaches nor creates a path.
    drift = project_catalog(
        {"methods": [entry]}, owners={"other.method": "other.owner"}
    )
    assert "tags" not in drift["paths"]["/daemon/request/demo.read"]["post"]
    assert "/daemon/request/other.method" not in drift["paths"]

    # A declaration source without a usable owner is omitted, not filled in.
    assert owners_from_document(
        {"methods": [{"name": "demo.read"}, {"name": "demo.write", "owner": ""}]}
    ) == {}

    # A non-string owner in the adapter is drift and must be refused, not fallback.
    with pytest.raises(ProjectionError):
        project_catalog({"methods": [entry]}, owners={"demo.read": 42})  # type: ignore[dict-item]

    # An explicit invalid owner on the Route-shaped entry is refused even when
    # the adapter has a usable owner; it is never silently replaced.
    invalid = _entry(owner="Bad Owner")
    with pytest.raises(ProjectionError):
        project_catalog(
            {"methods": [invalid]}, owners={"demo.read": "declared.demo"}
        )


def test_empty_declared_error_description_is_legal() -> None:
    entry = _entry(
        errors=[{"code": "INVALID_ARGUMENT", "description": "", "dataSchema": None}]
    )
    method = HttpMethod.from_catalog_entry(entry)
    operation = build_openapi((method,))["paths"]["/daemon/request/demo.read"]["post"]
    assert operation["x-hyprial-errors"][0]["description"] == ""


def test_shared_status_text_defers_dispatch_to_the_envelope() -> None:
    for status in (400, 403):
        text = STATUS_DESCRIPTIONS[status]
        assert "nothing was dispatched" not in text
        assert "dispatch" in text.lower()

    document = build_openapi(_methods())
    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    declared = operation["responses"]["default"]["content"]["application/json"][
        "schema"
    ]
    business_failure = {
        "schemaVersion": 1,
        "ok": False,
        "requestId": "probe-business",
        "method": "demo.write",
        "code": "INVALID_ARGUMENT",
        "message": "handler rejected the value",
        "dispatch": "dispatched",
        "data": {"field": "agent"},
    }
    Draft202012Validator(declared).validate(business_failure)
    structural_failure = {
        "schemaVersion": 1,
        "ok": False,
        "requestId": "probe-structural",
        "code": "INVALID_ARGUMENT",
        "message": "schema refused the body",
        "dispatch": "not-dispatched",
        "data": {"field": "agent"},
    }
    Draft202012Validator(
        document["components"]["schemas"]["ErrorEnvelope"]
    ).validate(structural_failure)


def test_envelope_auth_errors_and_declared_errors_are_one_projection() -> None:
    document = build_openapi(_methods())
    assert document["openapi"] == OPENAPI_VERSION
    assert document["info"]["version"] == str(HTTP_CONTRACT_VERSION)
    scheme = document["components"]["securitySchemes"][BEARER_SCHEME]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert "server-configured" in scheme["description"]

    error_envelope = document["components"]["schemas"]["ErrorEnvelope"]
    assert set(error_envelope["required"]) == {
        "schemaVersion",
        "ok",
        "requestId",
        "code",
        "message",
        "dispatch",
    }
    assert error_envelope["properties"]["dispatch"]["enum"] == list(DISPATCH_STATES)
    assert error_envelope["properties"]["ok"] == {"const": False}

    operation = document["paths"]["/daemon/request/demo.write"]["post"]
    success = document["components"]["schemas"]["success.demo.write"]
    assert success["properties"]["result"] == {
        "$ref": "#/components/schemas/result.demo.write"
    }
    assert success["properties"]["method"] == {"const": "demo.write"}
    assert success["properties"]["dispatch"] == {"const": "dispatched"}
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/success.demo.write"
    }

    for status in ("400", "401", "403", "404", "408", "409", "413", "429", "503", "504"):
        assert operation["responses"][status]["content"]["application/json"] == {
            "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
        }

    default = operation["responses"]["default"]
    variants = default["content"]["application/json"]["schema"]["oneOf"]
    by_code = {
        variant["properties"]["code"]["const"]: variant for variant in variants
    }
    assert set(by_code) == {"INVALID_ARGUMENT", "CALLER_NOT_AUTHORIZED"}
    assert by_code["INVALID_ARGUMENT"]["properties"]["data"] == ERRORS[0]["dataSchema"]
    assert by_code["CALLER_NOT_AUTHORIZED"]["properties"]["data"] == {}
    assert operation["x-hyprial-errors"] == [
        {
            "code": "INVALID_ARGUMENT",
            "description": ERRORS[0]["description"],
            "dataSchema": ERRORS[0]["dataSchema"],
        },
        {
            "code": "CALLER_NOT_AUTHORIZED",
            "description": ERRORS[1]["description"],
            "dataSchema": None,
        },
    ]


def test_transport_error_table_is_complete_and_unique() -> None:
    codes = [error.code for error in TRANSPORT_ERRORS]
    statuses = {error.status for error in TRANSPORT_ERRORS}
    assert len(codes) == len(set(codes))
    assert {"INVALID_ARGUMENT", "HTTP_UNAUTHORIZED", "HTTP_TIMEOUT"} <= set(codes)
    assert {400, 401, 403, 404, 408, 409, 413, 429, 500, 502, 503, 504} <= statuses
    for error in TRANSPORT_ERRORS:
        assert 400 <= error.status < 600, error.code
        assert error.description
    document = build_openapi(_methods())
    listed = {
        entry["code"] for entry in document["x-hyprial-http-contract"]["transportErrors"]
    }
    assert listed == set(codes)


def test_projection_is_deterministic_order_independent_and_volatile_free() -> None:
    forward = _methods()
    first = canonical_json(build_openapi(forward))
    second = canonical_json(build_openapi(tuple(reversed(forward))))
    assert first == second
    assert canonical_json(json.loads(first)) == first
    assert document_digest(build_openapi(forward)) == document_digest(
        build_openapi(tuple(reversed(forward)))
    )
    for forbidden in (
        "nowMs",
        "epoch",
        "pid",
        "HYPRIAL_HOME",
        "/Users/",
        "127.0.0.1",
        "localhost",
    ):
        assert forbidden not in first


@pytest.mark.parametrize(
    "entry",
    [
        _write_entry(retry="safe"),
        _entry(retry="never"),
        _entry(mode="cursor-poll"),
        _write_entry(confirmation="preview"),
        _write_entry(audience=["public"]),
        _write_entry(audience=[]),
        _write_entry(
            httpBinding={"identity": {"mode": "magic", "param": "agent"}, "readiness": None}
        ),
        _write_entry(
            httpBinding={
                "identity": {"mode": "local-owner-user-selector", "param": "missing"},
                "readiness": None,
            }
        ),
        _write_entry(
            httpBinding={
                "identity": {"mode": "local-owner-user-selector", "param": "clear"},
                "readiness": None,
            }
        ),
        _entry(mutates="false"),
    ],
)
def test_mislabeled_or_unsupported_entries_are_refused(
    entry: dict[str, object],
) -> None:
    with pytest.raises(ProjectionError):
        HttpMethod.from_catalog_entry(entry)


def test_duplicate_method_names_are_refused() -> None:
    method = HttpMethod.from_catalog_entry(_entry())
    with pytest.raises(ProjectionError):
        build_openapi((method, method))


def test_unknown_methods_are_never_invented() -> None:
    document = build_openapi(_methods())
    assert set(document["paths"]) == {
        "/healthz",
        "/readyz",
        "/daemon/request",
        "/openapi.json",
        "/daemon/request/demo.read",
        "/daemon/request/demo.write",
    }
    assert "demo.other" not in canonical_json(document)
    summary = document["x-hyprial-http-contract"]["methods"]
    assert [item["name"] for item in summary] == ["demo.read", "demo.write"]
    assert [item["callProfile"] for item in summary] == ["read", "write"]


def test_fixed_transport_paths_keep_their_authentication_boundary() -> None:
    document = build_openapi(_methods())
    assert "security" not in document["paths"]["/healthz"]["get"]
    for path in ("/readyz", "/daemon/request", "/openapi.json"):
        assert document["paths"][path]["get"]["security"] == [{BEARER_SCHEME: []}]
    health = document["paths"]["/healthz"]["get"]["responses"]["200"]
    assert health["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HealthResponse"
    }
    ready = document["paths"]["/readyz"]["get"]["responses"]["503"]
    assert ready["content"]["application/json"]["schema"] == {
        "oneOf": [
            {"$ref": "#/components/schemas/ReadyResponse"},
            {"$ref": "#/components/schemas/ErrorEnvelope"},
        ]
    }


def test_every_component_schema_and_local_ref_is_usable_offline() -> None:
    document = build_openapi(_methods())
    text = canonical_json(document)
    refs = set(re.findall(r'"\$ref":"#/components/schemas/([^"]+)"', text))
    assert refs == set(document["components"]["schemas"])
    for schema in document["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


def test_catalog_document_projection_matches_and_rejects_version_drift() -> None:
    methods = [_write_entry(), _entry()]
    document = project_catalog({"methods": methods})
    assert canonical_json(document) == canonical_json(
        build_openapi(
            (
                HttpMethod.from_catalog_entry(methods[1]),
                HttpMethod.from_catalog_entry(methods[0]),
            )
        )
    )
    with pytest.raises(ProjectionError):
        project_catalog({"httpContractVersion": 2, "methods": methods})
    with pytest.raises(ProjectionError):
        project_catalog({"methods": "not-a-list"})


def test_offline_export_writes_the_same_canonical_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = tmp_path / "catalog.json"
    entries = [_write_entry(), _entry()]
    for entry in entries:
        entry.pop("owner")
    catalog.write_text(
        json.dumps({"ok": True, "methods": entries}), encoding="utf-8"
    )
    owners = tmp_path / "owners.json"
    owners.write_text(
        json.dumps(
            {"methods": [{"name": "demo.read", "owner": "demo"}, {"name": "demo.write", "owner": "demo"}]}
        ),
        encoding="utf-8",
    )
    output = tmp_path / "openapi.json"
    assert (
        main(
            [
                "--catalog",
                str(catalog),
                "--owners",
                str(owners),
                "--out",
                str(output),
                "--digest",
            ]
        )
        == 0
    )
    text = output.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text == canonical_json(json.loads(text)) + "\n"
    assert document_digest(json.loads(text)) in capsys.readouterr().err
    document = json.loads(text)
    assert document["paths"].keys() == {
        "/healthz",
        "/readyz",
        "/daemon/request",
        "/openapi.json",
        "/daemon/request/demo.read",
        "/daemon/request/demo.write",
    }
    assert document["paths"]["/daemon/request/demo.read"]["post"]["tags"] == [
        "demo"
    ]


def test_declared_error_codes_accept_upper_and_frozen_lower_kebab() -> None:
    for code in ("INVALID_ARGUMENT", "ambiguous-space"):
        entry = _entry(
            errors=[{"code": code, "description": "", "dataSchema": None}]
        )
        document = build_openapi((HttpMethod.from_catalog_entry(entry),))
        operation = document["paths"]["/daemon/request/demo.read"]["post"]
        assert operation["x-hyprial-errors"][0]["code"] == code
        default = operation["responses"]["default"]["content"]["application/json"][
            "schema"
        ]
        assert default["oneOf"][0]["properties"]["code"] == {"const": code}


def test_an_unfrozen_lowercase_error_code_is_rejected() -> None:
    entry = _entry(
        errors=[
            {"code": "orgfs-import-missing", "description": "", "dataSchema": None}
        ]
    )
    with pytest.raises(ProjectionError, match="invalid declared error code"):
        HttpMethod.from_catalog_entry(entry)
