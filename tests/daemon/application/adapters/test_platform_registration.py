"""Registration saves through the existing adapter owner before runtime reload."""

import json
import stat

import pytest

from hyprial.daemon.impl.bootstrap import adapter_registration
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError
from tests.daemon.application.adapters.test_daemon_adapter_reload import _application, _close_application


@pytest.fixture
def registration_app(tmp_path):
    app = _application(tmp_path)
    try:
        yield app
    finally:
        _close_application(app)


def request(name="demo", secret="synthetic-app-secret"):
    return {"provider": "lark", "name": name, "appId": "cli_test", "appSecret": secret}


def test_registration_persists_private_files_and_reloads_without_starting(registration_app):
    app = registration_app
    result = app.handle("adapter.register", request())
    assert result == {
        "ok": True, "adapter": {"provider": "lark", "name": "demo", "appId": "cli_test"},
        "configured": True, "reloaded": True, "nextAction": "none",
    }
    secret = app.hyprial_home / "secrets/lark-demo.json"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert json.loads(secret.read_text()) == {"appSecret": "synthetic-app-secret"}
    assert json.loads((app.hyprial_home / "channels.json").read_text())["gateways"][0]["routes"] == []
    rows = app.handle("adapter.list", {})["adapters"]
    assert len(rows) == 1 and rows[0]["name"] == "demo"
    assert rows[0]["configured"] and not rows[0]["processRunning"] and not rows[0]["desired"]
    assert "synthetic-app-secret" not in json.dumps(result)
    assert str(app.hyprial_home) not in json.dumps(result)


def test_duplicate_registration_never_overwrites_credentials(registration_app):
    app = registration_app
    app.handle("adapter.register", request())
    paths = [app.hyprial_home / "channels.json", app.hyprial_home / "secrets/lark-demo.json"]
    before = [path.read_bytes() for path in paths]
    with pytest.raises(DaemonRequestError) as error:
        app.handle("adapter.register", request(secret="replacement-must-not-land"))
    assert error.value.code == "ADAPTER_EXISTS"
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize("patch,origin,code", [
    ({}, "worker", "CALLER_NOT_AUTHORIZED"),
    ({"actor": "forged"}, None, "INVALID_ARGUMENT"),
    ({"force": True}, None, "INVALID_ARGUMENT"),
    ({"provider": "whatsapp"}, None, "INVALID_ARGUMENT"),
])
def test_registration_admits_before_touching_files(registration_app, monkeypatch, patch, origin, code):
    monkeypatch.setattr(adapter_registration, "add_lark_gateway", lambda **kw: pytest.fail("registration before admission"))
    with pytest.raises(DaemonRequestError) as error:
        registration_app.handle("adapter.register", {**request(), **patch}, _trusted_message_origin=origin)
    assert error.value.code == code


def test_invalid_name_is_rejected_without_creating_files(registration_app):
    app = registration_app
    before = (app.hyprial_home / "channels.json").read_bytes()
    with pytest.raises(DaemonRequestError) as error:
        app.handle("adapter.register", request(name="../outside"))
    assert error.value.code == "INVALID_ARGUMENT"
    assert (app.hyprial_home / "channels.json").read_bytes() == before


def test_reload_failure_returns_saved_receipt_and_can_be_recovered(registration_app, monkeypatch):
    app = registration_app
    real_reload = app._ipc_adapter_reload

    def fail_reload():
        raise RuntimeError("secret=synthetic-app-secret /private/config/path")

    monkeypatch.setattr(app, "_ipc_adapter_reload", fail_reload)
    result = app.handle("adapter.register", request())
    assert result["configured"] is True
    assert result["reloaded"] is False and result["nextAction"] == "reload"
    assert "synthetic-app-secret" not in json.dumps(result)
    assert app.handle("adapter.list", {})["adapters"] == []
    monkeypatch.setattr(app, "_ipc_adapter_reload", real_reload)
    assert app.handle("adapter.reload", {})["added"] == ["demo"]
    assert app.handle("adapter.list", {})["adapters"][0]["name"] == "demo"


def test_storage_failure_is_unknown_and_not_automatically_replayed(registration_app, monkeypatch):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise OSError("synthetic-app-secret /private/config/path")

    monkeypatch.setattr(adapter_registration, "add_lark_gateway", fail)
    with pytest.raises(DaemonRequestError) as error:
        registration_app.handle("adapter.register", request())
    assert error.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert len(calls) == 1
    assert "synthetic-app-secret" not in str(error.value)


def test_registration_declares_http_write_without_retry():
    binding = METHOD_REGISTRY.get("adapter.register")
    assert binding is not None
    assert binding.spec.http_exposed and binding.spec.mutates
    assert binding.spec.retry == "never"
    assert binding.spec.audience == ("local-operator",)


def test_real_http_registration_then_list(registration_app, tmp_path, monkeypatch):
    from hyprial.shell.impl.request_http.server import RequestHttpServer
    from tests.shell.request_http.test_agent_reference import _config, _fake_request_once, _post

    calls = []
    discovery = _fake_request_once([])

    def dispatch(method, params, **kwargs):
        if method in {"ping", "daemon.catalog"}:
            return discovery(method, params, **kwargs)
        calls.append(method)
        return registration_app.handle(method, params)

    server = RequestHttpServer(_config(tmp_path), request_once=dispatch)
    server.start()
    try:
        for token, origin, status_code in [(None, None, 401), ("operator-token", "https://evil.example", 403)]:
            kwargs = {"token": token} if origin is None else {"origin": origin}
            status, body = _post(server, "adapter.register", request(), **kwargs)
            assert status == status_code and body["dispatch"] == "not-dispatched"
        assert calls == []
        status, body = _post(server, "adapter.register", request())
        assert status == 200 and body["result"]["reloaded"] is True
        status, body = _post(server, "adapter.list", {})
        assert status == 200 and body["result"]["adapters"][0]["name"] == "demo"
        assert calls == ["adapter.register", "adapter.list"]
    finally:
        server.shutdown()
