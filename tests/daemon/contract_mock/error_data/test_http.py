"""Real loopback listener with synthetic registered handler errors."""

import json

import pytest

from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import DaemonRequestError, export_catalog
from hyprial.shell.impl.request_http import server as server_module
from tests.shell.request_http.test_server import _config, _fake, _http

from .helpers import declaration


@pytest.mark.parametrize("data", [None, {}, {"field": "summary"}])
def test_http_preserves_optional_error_data_and_never_retries(tmp_path, monkeypatch, data):
    spec = declaration()
    rows = [{**export_catalog((spec,))["methods"][0], "state": "available",
             "declared": True, "callable": True, "reason": None}]
    snapshot, _ = _fake(rows)
    calls = []

    def refuse(app, params, **kwargs):
        calls.append(dict(params))
        raise DaemonRequestError("INVALID_ARGUMENT", "synthetic refusal", data)

    registry = MethodRegistry.from_bindings(((spec, refuse),))

    def request(method, params, **kwargs):
        if method in {"ping", "daemon.catalog"}:
            return snapshot(method, params, **kwargs)
        return registry.invoke(None, method, params)

    monkeypatch.setattr(server_module, "local_daemon_catalog", lambda: {"methods": rows})
    listener = server_module.RequestHttpServer(_config(tmp_path), request_once=request)
    listener.start()
    try:
        status, _, raw = _http(listener, "POST", "/daemon/request/probe.error", body={}, token="test")
        payload = json.loads(raw)
        assert status == 400
        assert (payload["code"], payload["dispatch"]) == ("INVALID_ARGUMENT", "dispatched")
        assert ("data" in payload) == (data is not None)
        if data is not None:
            assert payload["data"] == data
        assert calls == [{}]
        _, _, raw = _http(listener, "POST", "/daemon/request/probe.error", body={"extra": 1}, token="test")
        payload = json.loads(raw)
        assert payload["dispatch"] == "not-dispatched"
        assert "data" not in payload
        assert calls == [{}]
    finally:
        listener.shutdown()
