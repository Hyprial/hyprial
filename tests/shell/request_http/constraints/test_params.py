"""HTTP-only constraints reject before dispatch and pin the runtime catalog."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from hyprial.kernel import export_catalog
from hyprial.shell.impl.request_http import server as server_module
from hyprial.shell.impl.request_http.catalog import build_view, CatalogProblem
from hyprial.shell.impl.request_http.errors import HttpFailure
from tests.daemon.ipc.contracts.http_params.test_constraints import declaration, http_schema
from tests.daemon.ipc.contracts.http_params.test_formats import VALID_UUID, uuid_declaration
from tests.shell.request_http.test_catalog import PING, _catalog
from tests.shell.request_http.test_server import _config, _fake, _http


def entries(spec):
    return [{**export_catalog((spec,))["methods"][0], "state": "available",
             "declared": True, "callable": True, "reason": None}]


def view(rows):
    return build_view(PING, _catalog(rows), socket_path="test-only.sock", local_entries=rows)


def test_openapi_runtime_and_digest_share_the_narrowed_schema():
    spec = declaration()
    narrowed = view(entries(spec))
    original = view(entries(replace(spec, http_binding=replace(spec.http_binding, params_constraints=None))))
    assert narrowed.routes[spec.name].params_schema == http_schema(spec)
    assert narrowed.declaration_digest != original.declaration_digest
    assert narrowed.http_contract_digest != original.http_contract_digest
    invalid = entries(spec)
    invalid[0]["httpBinding"]["paramsConstraints"]["required"] = ["sessionRef"]
    with pytest.raises(CatalogProblem) as error:
        view(invalid)
    assert error.value.reason == "HTTP_PARAMS_INVALID"


def test_real_http_listener_rejects_missing_key_without_dispatch_and_never_retries(tmp_path, monkeypatch):
    rows = entries(declaration())
    snapshot, _ = _fake(rows)
    writes = []

    def request(method, params, **kwargs):
        if method in {"ping", "daemon.catalog"}:
            return snapshot(method, params, **kwargs)
        writes.append(deepcopy(params))
        if params["key"] == "lost":
            raise TimeoutError("test response lost after dispatch")
        return {"ok": True}

    monkeypatch.setattr(server_module, "local_daemon_catalog", lambda: {"methods": rows})
    listener = server_module.RequestHttpServer(_config(tmp_path), request_once=request)
    listener.start()
    try:
        for params in ({}, {"key": None}, {"key": 5}, {"key": " "},
                       {"key": "ok", "sessionRef": "forged"}):
            status, _, raw = _http(listener, "POST", "/daemon/request/probe.submit", body=params, token="test")
            result = json.loads(raw)
            assert (status, result["code"], result["dispatch"]) == (400, "INVALID_ARGUMENT", "not-dispatched")
        assert writes == []
        status, _, raw = _http(listener, "POST", "/daemon/request/probe.submit", body={"key": "ok"}, token="test")
        assert status == 200 and json.loads(raw)["dispatch"] == "dispatched"
        assert writes == [{"key": "ok"}]
        _, _, raw = _http(listener, "POST", "/daemon/request/probe.submit", body={"key": "lost"}, token="test")
        assert json.loads(raw)["dispatch"] == "unknown"
        assert writes == [{"key": "ok"}, {"key": "lost"}]
        rows[0]["httpBinding"]["paramsConstraints"]["properties"]["key"]["minLength"] = 2
        with pytest.raises(HttpFailure) as stale:
            listener.refresh(force=True)
        assert stale.value.code == "CATALOG_STALE"
        assert stale.value.dispatch == "not-dispatched"
    finally:
        listener.shutdown()


def test_real_listener_enforces_format_before_dispatch(tmp_path, monkeypatch):
    rows = entries(uuid_declaration())
    snapshot, _ = _fake(rows)
    writes = []

    def request(method, params, **kwargs):
        if method in {"ping", "daemon.catalog"}:
            return snapshot(method, params, **kwargs)
        writes.append(deepcopy(params))
        return {"ok": True}

    monkeypatch.setattr(server_module, "local_daemon_catalog", lambda: {"methods": rows})
    listener = server_module.RequestHttpServer(_config(tmp_path), request_once=request)
    listener.start()
    try:
        status, _, raw = _http(listener, "POST", "/daemon/request/probe.submit",
                               body={"key": "not-a-uuid"}, token="test")
        rejected = json.loads(raw)
        assert (status, rejected["code"], rejected["dispatch"]) == (400, "INVALID_ARGUMENT", "not-dispatched")
        assert writes == []
        status, _, raw = _http(listener, "POST", "/daemon/request/probe.submit",
                               body={"key": VALID_UUID}, token="test")
        assert status == 200 and json.loads(raw)["dispatch"] == "dispatched"
        assert writes == [{"key": VALID_UUID}]
    finally:
        listener.shutdown()
