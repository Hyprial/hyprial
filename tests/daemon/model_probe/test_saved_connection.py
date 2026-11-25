"""Saved connection testing uses an authorized exact revision, never an alternate secret."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl import model_probe
from hyprial.identity import SecretResolver
from hyprial.kernel import DaemonRequestError, ipc_errors

METHOD = "agent.secret.provider-test"


@pytest.fixture
def saved(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    app = SimpleNamespace(hyprial_home=home, _agent_registry=SimpleNamespace())
    resolver = SecretResolver(home, app._agent_registry)
    params = {"entryId": "custom", "fieldName": "key", "value": "synthetic-saved-secret",
              "expectedRevision": 0, "connection": {"version": 1, "protocol": "openai-completions",
              "profile": "openai-chat-completions/v1", "endpoint": "https://model.example/v1"}}
    result = METHOD_REGISTRY.invoke(app, "agent.secret.provider-configure", params, trusted_message_origin="ipc")
    metadata = result["connections"]["key"]
    selected = {"entryId": "custom", "fieldName": "key",
                **{name: metadata[name] for name in ("connectionRef", "configRevision")}}
    return app, resolver, selected


def test_saved_probe_passes_exact_selection_without_returning_secret(saved, monkeypatch):
    app, resolver, selected = saved
    observed = []

    async def probe(params):
        observed.append(params)
        return {"ok": True, "outcome": "passed", "httpStatus": 200, "latencyMs": 1}

    monkeypatch.setattr(model_probe, "probe", probe)
    for model in ("deepseek-v4.1-flash-1017", "qwen3.7-flash", "hy4-flash"):
        result = METHOD_REGISTRY.invoke(app, METHOD, {"connection": selected, "model": model,
            "timeoutSeconds": 30}, trusted_message_origin="ipc")
        assert result == {"ok": True, "outcome": "passed", "httpStatus": 200, "latencyMs": 1}
        assert observed[-1] == {"provider": "custom", "secret": "synthetic-saved-secret",
            "endpoint": "https://model.example/v1", "protocol": "openai-completions",
            "model": model, "timeoutSeconds": 30}
    assert resolver.model_service_metadata("custom")["connections"]["key"]["configRevision"] == 1


def test_stale_saved_probe_fails_before_network(saved, monkeypatch):
    app, _, selected = saved

    async def unexpected(params):
        pytest.fail("stale selection must not send a request")

    monkeypatch.setattr(model_probe, "probe", unexpected)
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, METHOD, {"connection": dict(selected, configRevision=2),
            "model": "hy4-flash"}, trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.MODEL_CONFIGURATION_CONFLICT


@pytest.mark.parametrize("origin", [None, "worker", "daemon"])
def test_saved_probe_never_reads_credentials_before_admission(origin, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unauthorized caller must not read saved credentials")

    monkeypatch.setattr(SecretResolver, "model_connection_probe_input", unexpected)
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(), METHOD, {"connection": {}, "model": "hy4-flash"},
                               trusted_message_origin=origin)
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_catalog_opt_in_does_not_project_generic_connections_as_legacy_keys(saved):
    app, _, selected = saved
    method = "agent.secret.provider-list"
    legacy = METHOD_REGISTRY.invoke(app, method, {}, trusted_message_origin="ipc")
    assert legacy == {"ok": True, "entries": []}
    result = METHOD_REGISTRY.invoke(app, method, {"connectionVersion": 1}, trusted_message_origin="ipc")
    row = result["entries"][0]
    assert row["connections"]["key"]["connectionRef"] == selected["connectionRef"]
    assert "routes" not in row
    assert "synthetic-saved-secret" not in str(result)


@pytest.mark.parametrize("model", [" hy4-flash", "hy4-flash ", "\thy4-flash"])
def test_saved_probe_refuses_model_normalization(saved, monkeypatch, model):
    app, _, selected = saved

    async def unexpected(params):
        pytest.fail("ambiguous model ID must not reach the upstream")

    monkeypatch.setattr(model_probe, "probe", unexpected)
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, METHOD, {"connection": selected, "model": model},
                               trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT


def test_saved_timeout_is_unverified_and_not_authentication_failure(saved, monkeypatch):
    app, _, selected = saved
    calls = []

    async def timeout(params):
        calls.append(params)
        return {"ok": True, "outcome": "timeout", "httpStatus": None, "latencyMs": 30000}

    monkeypatch.setattr(model_probe, "probe", timeout)
    result = METHOD_REGISTRY.invoke(app, METHOD, {"connection": selected, "model": "hy4-flash",
        "timeoutSeconds": 30}, trusted_message_origin="ipc")
    assert result == {"ok": True, "outcome": "timeout", "httpStatus": None, "latencyMs": 30000,
                      "phase": "probe", "reason": "deadline-exceeded"}
    assert len(calls) == 1
