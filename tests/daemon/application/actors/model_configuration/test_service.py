"""The public configuration methods share one authority and never invoke a model."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from tests.daemon.application.actors.model_binding.test_connection import configured as configured
from tests.daemon.application.actors.model_binding.test_destroy import app as app
from tests.identity.agents.home.roots.lifecycle.support import _isolated_environment as _isolated_environment


def params(configured):
    app, _, _, _, cell = configured
    agent = app.agents.require("worker")
    current = app.desired_state.model_binding_store.status(agent.agent_id, agent.uri).binding
    config = current.configuration.to_json()
    config.pop("intentDigest")
    config["model"] = "qwen3.7-flash"
    return {"name": "worker", "operationId": "configure-next", "expectedBindingRevision": 1,
            "expectedCellRevision": cell.revision, "configuration": config}


def invoke(app, method, values, *, origin="ipc"):
    return METHOD_REGISTRY.invoke(app, "agent.model." + method, values,
                                  trusted_message_origin=origin)


def test_operator_configure_and_status_return_metadata_without_key(configured):
    app, _, _, _, cell = configured
    calls = []

    def update(cell_id, **kwargs):
        nonlocal cell
        assert kwargs["expect_revision"] == cell.revision
        cell = replace(cell, params=kwargs["params"], revision=cell.revision + 1)
        calls.append(cell_id)
        return cell

    app._cell_runtime = SimpleNamespace(
        lookup=lambda _: cell, update=update, health=lambda: SimpleNamespace(source_stale=False),
        identity=SimpleNamespace(authority=cell.authority),
    )
    request = params(configured)
    result = invoke(app, "configure", request)
    assert result["operation"]["phase"] == "finalized"
    assert result["binding"]["bindingRevision"] == 2
    assert result["binding"]["configuration"]["model"] == "qwen3.7-flash"
    assert "synthetic-bound-key" not in str(result)
    assert len(calls) == 1
    assert invoke(app, "configure", request)["operation"] == result["operation"]
    assert len(calls) == 1
    status = invoke(app, "status", {"name": "worker", "operationId": "configure-next"})
    assert status["cell"]["intentMatches"] is True
    assert status["runtime"] == "not-verified"
    assert app.desired_state.load().harnesses == ()


@pytest.mark.parametrize("origin", [None, "worker", "daemon"])
def test_worker_refused_before_target_or_secrets(configured, monkeypatch, origin):
    app = configured[0]
    request = params(configured)
    monkeypatch.setattr(app.agents, "get", lambda _: pytest.fail("no target read before operator admission"))
    with pytest.raises(DaemonRequestError) as error:
        invoke(app, "configure", request, origin=origin)
    assert error.value.code == "CALLER_NOT_AUTHORIZED"


def test_foreign_agent_refused_before_secret_reads(configured, monkeypatch):
    app = configured[0]
    request = params(configured)
    original = app.agents.get
    monkeypatch.setattr(app.agents, "get", lambda name: replace(original(name), owner="foreign"))
    with pytest.raises(DaemonRequestError) as error:
        invoke(app, "configure", request)
    assert error.value.code == "TARGET_NOT_AUTHORIZED"


def test_revoked_connection_is_not_saved_and_unsupported_combo_is_explicit(configured):
    app = configured[0]
    request = params(configured)
    app._agent_registry.revoke_secret_grant("worker", "model")
    with pytest.raises(DaemonRequestError) as error:
        invoke(app, "configure", request)
    assert error.value.code == "MODEL_AUTH_UNAVAILABLE"
    agent = app.agents.require("worker")
    assert app.desired_state.model_binding_store.status(agent.agent_id, agent.uri).pending is None
    request["configuration"]["harness"] = "claude"
    with pytest.raises(DaemonRequestError) as error:
        invoke(app, "configure", request)
    assert error.value.code == "MODEL_COMBINATION_UNSUPPORTED"


def test_status_observes_pending_without_reading_secrets_or_changing_state(configured, monkeypatch):
    from hyprial.daemon.impl.application.model_binding.configure import service

    app, _, _, _, cell = configured
    agent = app.agents.require("worker")
    store = app.desired_state.model_binding_store
    config = store.status(agent.agent_id, agent.uri).binding.configuration.to_json()
    store.prepare(agent.agent_id, agent.uri, "pending", 1, config, coordination={
        "schemaVersion": 1, "cellId": cell.cell_id, "expectedRevision": cell.revision,
        "previousIntent": {"kind": "pi", "model": "hy4-flash"}, "phase": "planned",
        "observedRevision": None,
    })
    before = app.desired_state.load()
    monkeypatch.setattr(service, "resolve_agent_secrets", lambda **_: pytest.fail("status must not read Keys"))
    result = invoke(app, "status", {"name": "worker"})
    assert result["operation"]["phase"] == "prepared"
    assert result["binding"]["bindingRevision"] == 1
    assert app.desired_state.load() == before


def test_historical_receipt_and_current_binding_are_distinct(configured):
    app = configured[0]
    agent = app.agents.require("worker")
    store = app.desired_state.model_binding_store
    config = store.status(agent.agent_id, agent.uri).binding.configuration.to_json()
    op = store.prepare(agent.agent_id, agent.uri, "next", 1, config)
    store.finalize(agent.agent_id, agent.uri, "next", op.request_digest, 1)
    result = invoke(app, "status", {"name": "worker", "operationId": "configure"})
    assert result["operation"]["resultBindingRevision"] == 1
    assert result["binding"]["bindingRevision"] == 2
