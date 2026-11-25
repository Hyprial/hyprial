"""Agent deletion confirms the previewed incarnation through the real application."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError
from tests.identity.agents.home.roots.lifecycle.support import (
    _application,
    _isolated_environment as _isolated_environment,
)


@pytest.fixture
def app(tmp_path):
    application = _application(tmp_path)
    application.handle("agent.create", {"name": "worker"})
    return application


def call(app, method, params):
    return app.handle(method, params, _trusted_message_origin="ipc")


def test_preview_and_execute_are_registered_http_methods():
    for name, mode in (("agent.destroy.preview", "preview"), ("agent.destroy", "execute")):
        bound = METHOD_REGISTRY.get(name)
        assert bound is not None
        assert bound.spec.http_exposed
        assert bound.spec.confirmation == mode
    execute = METHOD_REGISTRY.get("agent.destroy").spec
    assert set(execute.params_schema["properties"]) == {"operationId"}
    assert execute.params_schema["additionalProperties"] is False
    assert execute.retry == "never"


def test_preview_does_not_delete_and_confirmation_replays_once(app, monkeypatch):
    preview = call(app, "agent.destroy.preview", {"name": "worker"})
    assert preview["operationId"]
    assert preview["expiresAt"]
    assert preview["summary"]
    assert preview["workspace"]["exists"] is True
    assert app.agents.get("worker") is not None
    original = app._destroy_agent
    effects = []

    def destroy(*args, **kwargs):
        effects.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(app, "_destroy_agent", destroy)
    params = {"operationId": preview["operationId"]}
    result = call(app, "agent.destroy", params)
    assert result["destroyed"] is True
    assert app.agents.get("worker") is None
    assert call(app, "agent.destroy", params) == result
    assert len(effects) == 1


@pytest.mark.parametrize("params", [{"name": "worker"}, {"operationId": "unknown", "name": "worker"}])
def test_name_execute_and_parameter_replacement_have_no_effect(app, params):
    with pytest.raises(DaemonRequestError) as rejected:
        call(app, "agent.destroy", params)
    assert rejected.value.code == "INVALID_ARGUMENT"
    assert app.agents.get("worker") is not None


def test_unknown_operation_has_no_effect(app):
    with pytest.raises(DaemonRequestError) as rejected:
        call(app, "agent.destroy", {"operationId": "unknown"})
    assert rejected.value.code == "PREVIEW_UNKNOWN"
    assert app.agents.get("worker") is not None


def test_changed_incarnation_refuses_old_preview(app, monkeypatch):
    preview = call(app, "agent.destroy.preview", {"name": "worker"})
    app._agent_registry.rotate_secret_authority("worker")
    effects = []
    monkeypatch.setattr(app, "_stop_agent_runtime", lambda *a, **kw: effects.append("stop"))
    with pytest.raises(DaemonRequestError) as rejected:
        call(app, "agent.destroy", {"operationId": preview["operationId"]})
    assert rejected.value.code == "PREVIEW_STALE"
    assert effects == []
    assert app.agents.get("worker") is not None


def test_worker_relay_cannot_gain_destroy_access():
    from hyprial.kernel.impl.contracts.channels.worker_channel import (
        WorkerBinding, WorkerChannelError, bound_request,
    )

    binding = WorkerBinding("agent:owner:node:worker", "session", "epoch")
    for method in ("agent.destroy.preview", "agent.destroy"):
        with pytest.raises(WorkerChannelError) as rejected:
            bound_request({"version": 1, "method": method, "params": {}}, binding)
        assert rejected.value.code == "METHOD_NOT_FOUND"


def test_expired_preview_does_not_stop_agent(app, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(app._agent_destroy_previews, '_now_ms', lambda: clock[0])
    preview = call(app, 'agent.destroy.preview', {'name': 'worker'})
    clock[0] = preview['expiresAt']
    with pytest.raises(DaemonRequestError) as refused:
        call(app, 'agent.destroy', {'operationId': preview['operationId']})
    assert refused.value.code == 'PREVIEW_EXPIRED'
    assert app.agents.get('worker') is not None


@pytest.mark.parametrize('change', ['adapter-binding', 'routine'])
def test_changed_dependencies_require_new_preview(app, monkeypatch, change):
    from tests.support.identity.cell_runtime import bind_adapters, registry_cell_runtime

    registry_cell_runtime(app)
    preview = call(app, 'agent.destroy.preview', {'name': 'worker'})
    if change == 'adapter-binding':
        bind_adapters(app, app.agents.require('worker'), ('lark-isolated-test',))
    else:
        monkeypatch.setattr(app, '_routines_bound_to', lambda actor: [{'name': 'new', 'registrationId': 'r1'}])
    effects = []
    monkeypatch.setattr(app, '_stop_agent_runtime', lambda *a, **kw: effects.append('stop'))
    with pytest.raises(DaemonRequestError) as refused:
        call(app, 'agent.destroy', {'operationId': preview['operationId']})
    assert refused.value.code == 'PREVIEW_STALE'
    assert effects == []
    assert app.agents.get('worker') is not None


def test_claimed_failure_replays_without_repeating_effect(app, monkeypatch):
    preview = call(app, 'agent.destroy.preview', {'name': 'worker'})
    effects = []

    def stop(*args, **kwargs):
        effects.append('stop')
        raise DaemonRequestError('SUBMIT_OUTCOME_UNKNOWN', 'isolated failure')

    monkeypatch.setattr(app, '_stop_agent_runtime', stop)
    for _ in range(2):
        with pytest.raises(DaemonRequestError) as failed:
            call(app, 'agent.destroy', {'operationId': preview['operationId']})
        assert failed.value.code == 'SUBMIT_OUTCOME_UNKNOWN'
    assert effects == ['stop']
    assert app._agent_destroy_previews._records[preview['operationId']].completed


def test_gui_confirmation_is_bound_to_admitted_view(app):
    from hyprial.daemon.impl.application.onboarding import admitted_onboarding_context
    from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface

    def context(view):
        return CallContext(Surface('home', 'control', view, 'generation'), 'serving', 'local-gui')

    with admitted_onboarding_context(context('one')):
        preview = call(app, 'agent.destroy.preview', {'name': 'worker'})
    with admitted_onboarding_context(context('two')):
        with pytest.raises(DaemonRequestError) as refused:
            call(app, 'agent.destroy', {'operationId': preview['operationId']})
    assert refused.value.code == 'PREVIEW_STALE'
    assert app.agents.get('worker') is not None


@pytest.mark.parametrize('origin,identity', [('worker', {}), ('ipc', {'actor': 'worker'}), ('ipc', {'sessionRef': 'fake'})])
def test_worker_and_identity_claims_do_not_become_operator(app, origin, identity):
    with pytest.raises(DaemonRequestError) as refused:
        app.handle('agent.destroy.preview', {'name': 'worker', **identity}, _trusted_message_origin=origin)
    assert refused.value.code == 'CALLER_NOT_AUTHORIZED'
    assert app.agents.get('worker') is not None
