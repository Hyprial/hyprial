"""The request reads bounded platform observations without granting scopes."""

import pytest

from hyprial.daemon.impl.adapters.lark.credentials.authorization import inventory
from hyprial.kernel import DaemonRequestError
from tests.daemon.application.adapters.test_daemon_adapter_reload import _application, _close_application


@pytest.fixture
def app(tmp_path):
    value = _application(tmp_path)
    value.handle("adapter.register", {
        "provider": "lark", "name": "demo", "appId": "cli_test", "appSecret": "synthetic-only",
    })
    try:
        yield value
    finally:
        _close_application(value)


@pytest.fixture
def observations(monkeypatch):
    response = {"code": 0, "data": {"scopes": [
        {"scope_name": "im:message", "grant_status": 1, "scope_type": "tenant"},
    ]}}
    calls = []

    class Client:
        def list_scopes(self):
            calls.append("list_scopes")
            return response

    def configured(home, state_dir, adapter, *, http_timeout_seconds):
        assert adapter == "demo" and http_timeout_seconds == 2.0
        return "cli_test", Client()

    monkeypatch.setattr(inventory, "configured_scope_client", configured)
    monkeypatch.setattr(inventory.time, "time", lambda: 1700000000.25)
    return response, calls


def test_status_projects_observed_grants_and_milliseconds(app, observations):
    result = app.handle("adapter.authorization.status", {"name": "demo"})
    assert result == {
        "ok": True, "adapter": "demo", "appId": "cli_test", "state": "unauthorized",
        "capabilities": ["message-send", "receive-p2p", "quote-fetch"], "missingCapabilities": ["receive-group-mention"],
        "checkedAt": 1700000000250,
    }
    assert observations[1] == ["list_scopes"]


def test_failed_observation_is_not_misreported_as_no_permissions(app, observations):
    response, calls = observations
    response.update(code=999, msg="secret=synthetic-only /private/path")
    result = app.handle("adapter.authorization.status", {"name": "demo"})
    assert result["state"] == "unconfirmed"
    assert result["missingCapabilities"] == []
    assert result["reason"] == "SCOPE_QUERY_FAILED"
    assert "synthetic-only" not in str(result) and calls == ["list_scopes"]


@pytest.mark.parametrize("params,origin,code", [
    ({"name": "demo"}, "worker", "CALLER_NOT_AUTHORIZED"),
    ({"name": "demo", "actor": "forged"}, None, "INVALID_ARGUMENT"),
    ({"name": "absent"}, None, "ADAPTER_NOT_FOUND"),
])
def test_admission_precedes_scope_client(app, monkeypatch, params, origin, code):
    monkeypatch.setattr(inventory, "configured_scope_client", lambda *args, **kw: pytest.fail("unexpected network"))
    with pytest.raises(DaemonRequestError) as error:
        app.handle("adapter.authorization.status", params, _trusted_message_origin=origin)
    assert error.value.code == code


def test_real_http_reads_observation_without_scope_application(app, observations, tmp_path):
    from hyprial.shell.impl.request_http.server import RequestHttpServer
    from tests.shell.request_http.test_agent_reference import _config, _fake_request_once, _post

    discovery = _fake_request_once([])
    calls = []

    def dispatch(method, params, **kwargs):
        if method in {"ping", "daemon.catalog"}:
            return discovery(method, params, **kwargs)
        calls.append(method)
        return app.handle(method, params)

    server = RequestHttpServer(_config(tmp_path), request_once=dispatch)
    server.start()
    try:
        status, body = _post(server, "adapter.authorization.status", {"name": "demo"})
        assert status == 200 and body["result"]["state"] == "unauthorized"
        assert body["result"]["checkedAt"] == 1700000000250
        assert calls == ["adapter.authorization.status"]
        assert observations[1] == ["list_scopes"]
    finally:
        server.shutdown()


def test_scope_client_uses_bounded_socket_timeout_and_only_reads(monkeypatch):
    import io
    import json
    from hyprial.daemon.impl.adapters.lark.credentials import scopes

    calls = []

    def urlopen(request, *, timeout):
        calls.append((request.full_url, request.method, timeout))
        result = {"tenant_access_token": "synthetic-token"} if request.method == "POST" else {"code": 0, "data": {"scopes": []}}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(scopes.urllib.request, "urlopen", urlopen)
    client = scopes.LarkScopeClient("cli_test", "synthetic-only", origin="http://127.0.0.1:1", http_timeout_seconds=2.0)
    assert client.list_scopes()["code"] == 0
    assert calls == [
        ("http://127.0.0.1:1/open-apis/auth/v3/tenant_access_token/internal", "POST", 2.0),
        ("http://127.0.0.1:1/open-apis/application/v6/scopes", "GET", 2.0),
    ]
