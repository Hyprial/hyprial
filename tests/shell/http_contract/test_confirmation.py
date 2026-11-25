"""Preview confirmation stays bound to the declared pair over HTTP."""

from copy import deepcopy

import pytest

from hyprial.shell.impl.http_contract import HttpMethod, ProjectionError, build_openapi
from hyprial.shell.impl.request_http.catalog import declaration_digest
from tests.shell.http_contract.test_projection import _entry
from tests.shell.request_http.test_catalog import _view


def pair():
    preview = _entry(
        "demo.remove.preview", declared=True, httpExposed=True,
        confirmation="preview",
        preview={
            "operationField": "operationId", "expiresField": "expiresAt",
            "summaryField": "summary", "ttlSeconds": 300,
            "binds": ["caller", "method", "paramsDigest", "expiry"],
        },
        resultSchema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "operationId": {"type": "string", "minLength": 1},
                "expiresAt": {"type": "integer"}, "summary": {"type": "string"},
            },
            "required": ["operationId", "expiresAt", "summary"],
        },
    )
    execute = _entry(
        "demo.remove", declared=True, httpExposed=True,
        mutates=True, retry="never", confirmation="execute", previewOf=preview["name"],
        paramsSchema={
            "type": "object", "additionalProperties": False,
            "properties": {"operationId": {"type": "string", "minLength": 1}},
            "required": ["operationId"],
        },
    )
    return preview, execute


def test_catalog_and_openapi_preserve_preview_binding_without_replacement_params():
    preview, execute = pair()
    view = _view([preview, execute])
    assert set(view.routes) == {preview["name"], execute["name"]}
    exported = [route.public_json() for route in view.routes.values()]
    assert exported[0]["preview"] == preview["preview"]
    assert exported[1]["previewOf"] == preview["name"]
    methods = [HttpMethod.from_catalog_entry(entry) for entry in exported]
    document = build_openapi(methods)
    pre = document["paths"]["/daemon/request/demo.remove.preview"]["post"]
    run = document["paths"]["/daemon/request/demo.remove"]["post"]
    assert pre["x-hyprial-preview"] == preview["preview"]
    assert run["x-hyprial-preview-of"] == preview["name"]
    assert run["x-hyprial-retry"] == "never"
    assert document["components"]["schemas"]["params.demo.remove"] == execute["paramsSchema"]


def test_confirmation_metadata_is_bound_by_both_digests():
    entries = list(pair())
    changed = deepcopy(entries)
    changed[0]["preview"]["ttlSeconds"] = 60
    assert declaration_digest(entries) != declaration_digest(changed)
    assert _view(entries).http_contract_digest != _view(changed).http_contract_digest


@pytest.mark.parametrize("change", [
    {"previewOf": "absent.preview"},
    {"mutates": False, "retry": "safe"},
    {"paramsSchema": {"type": "object", "properties": {}, "additionalProperties": True}},
    {"httpBinding": {"identity": {"mode": "local-owner-selector", "param": "operationId"}}},
])
def test_invalid_execute_is_not_callable_and_cannot_be_projected(change):
    preview, execute = pair()
    execute.update(change)
    view = _view([preview, execute])
    assert execute["name"] not in view.routes
    with pytest.raises(ProjectionError):
        build_openapi([HttpMethod.from_catalog_entry(entry) for entry in (preview, execute)])


@pytest.mark.parametrize("change", [
    {"preview": None}, {"preview": {"ttlSeconds": 300}},
    {"mutates": True, "retry": "never"},
    {"resultSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
])
def test_invalid_preview_cannot_admit_its_execute(change):
    preview, execute = pair()
    preview.update(change)
    assert not _view([preview, execute]).routes
    with pytest.raises(ProjectionError):
        build_openapi([HttpMethod.from_catalog_entry(entry) for entry in (preview, execute)])


def test_hidden_preview_cannot_admit_execute():
    preview, execute = pair()
    preview["httpExposed"] = False
    assert not _view([preview, execute]).routes
    with pytest.raises(ProjectionError):
        build_openapi([HttpMethod.from_catalog_entry(execute)])


def test_http_forbidden_operation_id_excludes_execute_without_breaking_catalog():
    preview, execute = pair()
    execute["paramsSchema"]["properties"]["operationId"]["x-hyprial-http-forbidden"] = True
    view = _view([preview, execute])
    assert set(view.routes) == {preview["name"]}
    with pytest.raises(ProjectionError):
        build_openapi([HttpMethod.from_catalog_entry(entry) for entry in (preview, execute)])
