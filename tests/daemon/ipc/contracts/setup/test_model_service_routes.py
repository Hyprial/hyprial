"""Only an operator can save model routing; grants derive their own metadata."""

import json

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.identity import SecretResolver, SecretSource
from hyprial.kernel import DaemonRequestError
from tests.daemon.ipc.contracts.agent.test_read_specs import app as app

METHOD = "agent.secret.provider-configure"
VALUE = "synthetic-routing-key-never-returned"
PARAMS = {"entryId": "gateway", "fieldName": "key", "value": VALUE,
          "endpoint": "https://model.example/v1", "protocol": "openai-completions",
          "modelProvider": "deepseek"}


def invoke(app, method=METHOD, params=None, origin="ipc"):
    return METHOD_REGISTRY.invoke(app, method, PARAMS if params is None else params,
                                  trusted_message_origin=origin)


def test_configure_catalog_rotate_and_grant_use_one_saved_record(app):
    result = invoke(app)
    route = {"endpoint": PARAMS["endpoint"], "protocol": PARAMS["protocol"], "vendorId": "deepseek"}
    assert result == {"ok": True, "entryId": "gateway", "fieldNames": ["key"], "routes": {"key": route}}
    listed = invoke(app, "agent.secret.provider-list", {})
    assert listed == {"ok": True, "entries": [{"entryId": "gateway", "fieldNames": ["key"], "routes": {"key": route}}]}
    app._agent_registry.create("worker")
    params = {"actor": "worker", "grantId": "model", "source": "user-provider",
              "entryId": "gateway", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"],
              "revision": 1, "routeRequired": False}
    invoke(app, "agent.secret.grant", params)
    resolver = SecretResolver(app.hyprial_home, app._agent_registry)
    assert resolver.resolve("worker", "model").grant.route_required is True
    changed = invoke(app, "agent.secret.provider-write", {"entryId": "gateway", "fieldName": "other", "value": "second"})
    assert changed["fieldNames"] == ["key", "other"]
    assert resolver.resolve("worker", "model").route.endpoint == PARAMS["endpoint"]
    assert VALUE not in json.dumps([result, listed, changed])


@pytest.mark.parametrize("configured", [True, False])
def test_internal_grant_derives_saved_route_and_resolves(app, configured):
    if configured:
        invoke(app)
    else:
        invoke(app, "agent.secret.provider-write", {
            "entryId": "gateway", "fieldName": "key", "value": VALUE,
        })
    app._agent_registry.create("worker")
    grant = app.agents.grant_secret(
        "worker", grant_id="model", source=SecretSource.USER_PROVIDER,
        entry_id="gateway", field_name="key",
        environment_names=("DEEPSEEK_API_KEY",), revision=1,
    )
    stored = app._agent_registry.secret_grant("worker", "model")
    assert grant.route_required is configured
    assert stored.route_required is configured
    resolved = SecretResolver(app.hyprial_home, app._agent_registry).resolve("worker", "model")
    assert resolved.value == VALUE
    if configured:
        assert resolved.route.endpoint == PARAMS["endpoint"]
    else:
        assert resolved.route is None


@pytest.mark.parametrize("extra", [{"routeRequired": True}, {"unexpected": VALUE}, {"value": "bad\nkey"},
                                   {"endpoint": "https://name:password@model.example/v1"},
                                   {"protocol": "anthropic-messages"}, {"modelProvider": "other"}])
def test_invalid_configure_is_refused_without_values_or_paths(app, extra):
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app, params={**PARAMS, **extra})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert VALUE not in str(caught.value)
    assert str(app.hyprial_home) not in str(caught.value)
    assert not (app.hyprial_home / "secrets").exists()


@pytest.mark.parametrize("origin,extra", [(None, {}), ("worker", {}), ("ipc", {"actor": "worker"}),
                                          ("ipc", {"sessionRef": "fake"})])
def test_operator_admission_precedes_schema_validation(app, origin, extra):
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app, params={"unexpected": VALUE, **extra}, origin=origin)
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"
    assert not (app.hyprial_home / "secrets").exists()


@pytest.mark.parametrize("gate", ["owner-keys", "damaged"])
def test_configure_does_not_bypass_another_authority(app, gate):
    (app.hyprial_home / "settings.json").write_text(json.dumps({"providerKeys": {"authority": gate}}))
    with pytest.raises(DaemonRequestError) as caught:
        invoke(app)
    assert caught.value.code == "AGENT_SECRET_RESOLUTION_FAILED"
    assert not (app.hyprial_home / "secrets").exists()


def test_configure_existing_route_can_preserve_value(app):
    invoke(app)
    params = {key: value for key, value in PARAMS.items() if key != "value"}
    result = invoke(app, params={**params, "endpoint": "https://other.example/v1"})
    assert result["routes"]["key"]["endpoint"] == "https://other.example/v1"
    path = app.hyprial_home / "secrets/providers/gateway.json"
    assert json.loads(path.read_text())["values"]["key"] == VALUE


@pytest.mark.parametrize("existing", ["none", "ordinary", "configured"])
def test_create_only_configure_never_replaces_an_existing_entry(app, existing):
    if existing == "ordinary":
        invoke(app, "agent.secret.provider-write", {
            "entryId": "gateway", "fieldName": "other", "value": "original",
        })
    elif existing == "configured":
        invoke(app)
    path = app.hyprial_home / "secrets/providers/gateway.json"
    before = path.read_bytes() if path.exists() else None
    params = {**PARAMS, "createOnly": True, "value": "replacement"}
    if existing == "none":
        assert invoke(app, params=params)["entryId"] == "gateway"
        assert json.loads(path.read_text())["values"]["key"] == "replacement"
    else:
        with pytest.raises(DaemonRequestError) as caught:
            invoke(app, params=params)
        assert caught.value.code == "INVALID_ARGUMENT"
        assert path.read_bytes() == before
        assert "replacement" not in str(caught.value)
        assert str(app.hyprial_home) not in str(caught.value)


def test_new_method_contract_is_closed_and_write_only():
    binding = METHOD_REGISTRY.get(METHOD)
    assert binding is not None
    assert binding.preauthorize is not None
    spec = binding.spec
    assert spec.params_schema["additionalProperties"] is False
    assert spec.params_schema["properties"]["value"]["writeOnly"] is True
    assert spec.http_exposed and spec.mutates and spec.retry == "never"
    assert spec.audience == ("local-operator",)
