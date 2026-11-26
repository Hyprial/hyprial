"""Create publishes explicit intent before defaults and reports partial effects."""

import pytest

from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.daemon.impl.application.model_binding.admission import pin
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from tests.daemon.application.actors.model_binding.test_destroy import app as app
from tests.identity.agents.home.roots.lifecycle.support import _isolated_environment as _isolated_environment


def request():
    return {"name": "new-native", "modelConfiguration": {"operationId": "initial-model", "configuration": {
        "authKind": "native", "nativeAuthRef": "native:v1:claude:oauth:anthropic",
        "harness": "claude", "model": "claude-sonnet-4-6", "mode": "headless"}}}


def test_unconfigured_create_is_durable_and_cannot_start_with_defaults(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app._agent_registry, "default_claude_credential", lambda: calls.append("default") or None)
    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(app, "agent.create", request(), trusted_message_origin="ipc")
    assert error.value.code == "MODEL_CONFIGURATION_INCOMPLETE"
    agent = app.agents.require("new-native")
    assert error.value.data["agentId"] == agent.agent_id
    assert agent.capabilities["modelConfigurationRequired"] is True
    assert not calls
    with pytest.raises(DaemonRequestError) as refused:
        pin(app, HarnessLaunchSpec("claude", "new-native", True))
    assert refused.value.code == "MODEL_CONFIGURATION_INCOMPLETE"
    assert refused.value.data["reason"] == "configuration-required"
    assert app.desired_state.load().harnesses == ()


def test_invalid_explicit_model_has_no_creation_effects(app):
    params = request()
    params["modelConfiguration"]["configuration"]["model"] = " model-with-padding "
    with pytest.raises(DaemonRequestError):
        METHOD_REGISTRY.invoke(app, "agent.create", params, trusted_message_origin="ipc")
    assert app.agents.get("new-native") is None


def test_worker_cannot_enter_explicit_create(app, monkeypatch):
    monkeypatch.setattr(app, "_handle_agent_create_with_default", lambda *_: pytest.fail("unauthorized creation"))
    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(app, "agent.create", request(), trusted_message_origin="worker")
    assert error.value.code == "CALLER_NOT_AUTHORIZED"


def test_mismatched_native_reference_fails_before_identity_creation(app):
    params = request()
    params["modelConfiguration"]["configuration"]["nativeAuthRef"] = "native:v1:pi:oauth:anthropic"
    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(app, "agent.create", params, trusted_message_origin="ipc")
    assert error.value.code == "MODEL_COMBINATION_UNSUPPORTED"
    assert app.agents.get("new-native") is None


def test_connection_create_grants_exact_saved_field_and_uses_coordinator(app, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from hyprial.identity import SecretResolver
    from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
    from tests.daemon.cells.runtime.cell_runtime_support import authority_for

    resolver = SecretResolver(app.hyprial_home, app._agent_registry)
    resolver.configure_model_connection("gateway", field_name="key", value="synthetic-create-key",
        expected_revision=0, connection={"version": 1, "endpoint": "https://gateway.example/v1",
        "protocol": "openai-completions", "profile": "openai-chat-completions/v1"})
    saved = resolver.model_service_metadata("gateway")["connections"]["key"]
    cells = {}
    authority = authority_for("user:owner")
    def lookup(cell_id):
        if cell_id not in cells:
            agent = next(agent for agent in app.agents.list() if agent.cell_id == cell_id)
            cells[cell_id] = Cell(cell_id, "agent", authority, CellScope.owner(),
                CellBinding.subject(agent.agent_id), members=(CellMember(agent.uri, "harness"),),
                params={"cell.members": {agent.uri: {"kind": "pi"}}})
        return cells[cell_id]
    def update(cell_id, **kwargs):
        before = lookup(cell_id)
        assert kwargs["expect_revision"] == before.revision
        cells[cell_id] = replace(before, params=kwargs["params"], revision=before.revision + 1)
        return cells[cell_id]
    app._cell_runtime = SimpleNamespace(lookup=lookup, update=update,
        health=lambda: SimpleNamespace(source_stale=False), identity=SimpleNamespace(authority=authority))
    monkeypatch.setattr(app, "_ipc_agent_start", lambda *_a, **_k: pytest.fail("configuration does not start"))
    result = METHOD_REGISTRY.invoke(app, "agent.create", {"name": "new-key",
        "modelConfiguration": {"operationId": "create-exact", "configuration": {
            "authKind": "connection", "harness": "pi", "mode": "headless", "model": "hy4-flash",
            "connectionRef": saved["connectionRef"], "configRevision": saved["configRevision"]}}},
        trusted_message_origin="ipc")
    assert result["created"] is True and result["providerDefault"] is None
    assert result["modelConfiguration"]["operation"]["phase"] == "finalized"
    assert result["modelConfiguration"]["cell"]["intentMatches"] is True
    assert "synthetic-create-key" not in str(result)
    agent = app.agents.require("new-key")
    grant, = app._agent_registry.secret_inventory(agent.actor)
    assert (grant.entry_id, grant.field_name) == ("gateway", "key")
    assert pin(app, HarnessLaunchSpec("pi", agent.actor, True)).model == "hy4-flash"


def test_internal_grant_fence_refuses_same_name_and_timestamp_new_incarnation(app):
    from hyprial.daemon.impl.application.actors.agents_admin.secret_fences import expected_secret_target

    agent = app.agents.require("worker")
    with pytest.raises(DaemonRequestError) as error:
        expected_secret_target(app, "agent.secret.grant", {"actor": agent.actor},
            {"uri": agent.uri, "createdAtMs": agent.created_at_ms, "agentId": "old-incarnation"})
    assert error.value.code == "AGENT_INCARNATION_MISMATCH"
