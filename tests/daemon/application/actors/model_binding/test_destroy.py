"""The real destroy path fences and cleans binding records by incarnation."""

import pytest

from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore
from hyprial.kernel import DaemonRequestError, DesiredStateError
from tests.identity.agents.home.roots.lifecycle.support import (
    _application,
    _isolated_environment as _isolated_environment,
)


@pytest.fixture
def app(tmp_path):
    result = _application(tmp_path)
    result.handle("agent.create", {"name": "worker"})
    return result


def prepare(app, agent, member=None):
    return app.desired_state.model_binding_store.prepare(
        agent.agent_id,
        member or agent.uri,
        "op-1",
        0,
        {
            "authKind": "connection",
            "connectionRef": "connection-1",
            "configRevision": 1,
            "model": "hy4-flash",
            "harness": "pi",
            "mode": "headless",
            "intentDigest": "a" * 64,
        },
    )


def test_destroy_purges_binding_and_receipts(app):
    agent = app.agents.require("worker")
    pending = prepare(app, agent)
    store = app.desired_state.model_binding_store
    store.finalize(agent.agent_id, agent.uri, "op-1", pending.request_digest, 0)
    assert app._destroy_agent("worker")["destroyed"] is True
    state = app.desired_state.load()
    assert state.model_bindings == ()
    assert state.model_binding_operations == ()
    assert state.model_agent_lifecycle == ()


def test_destroy_marker_exists_before_effect_without_a_binding(app):
    agent = app.agents.require("worker")
    observed = []

    def before_effect(_agent):
        marker = app.desired_state.model_binding_store.status(
            agent.agent_id, "other"
        ).cleanup
        observed.append(marker)
        with pytest.raises(DesiredStateError, match="destroy-cleanup-pending"):
            prepare(app, agent, "other")

    app._destroy_agent("worker", before_effect=before_effect)
    assert observed[0].entity_token == agent.entity_token


def test_marker_write_failure_precedes_all_destructive_effects(app, monkeypatch):
    agent = app.agents.require("worker")
    effects = []

    def fail(*args, **kwargs):
        raise OSError("synthetic marker failure")

    monkeypatch.setattr(ModelBindingStore, "mark_cleanup", fail)
    monkeypatch.setattr(
        app, "_stop_agent_runtime", lambda *a, **k: effects.append("stop")
    )
    with pytest.raises(DaemonRequestError) as caught:
        app._destroy_agent("worker", before_effect=lambda a: effects.append("before"))
    assert caught.value.code == "MODEL_CONFIGURATION_INCOMPLETE"
    assert effects == []
    assert app.agents.require("worker").agent_id == agent.agent_id


def test_failed_purge_reports_deleted_entity_and_retains_marker(app, monkeypatch):
    agent = app.agents.require("worker")
    prepare(app, agent)

    def fail(*args, **kwargs):
        raise OSError("synthetic purge failure")

    monkeypatch.setattr(ModelBindingStore, "purge_destroyed_agent", fail)
    with pytest.raises(DaemonRequestError) as caught:
        app._destroy_agent("worker")
    assert caught.value.code == "MODEL_CONFIGURATION_INCOMPLETE"
    assert caught.value.data == {
        "phase": "bind",
        "reason": "destroy-cleanup-pending",
        "entityDeleted": True,
    }
    assert app.agents.get("worker") is None
    status = app.desired_state.model_binding_store.status(agent.agent_id, agent.uri)
    assert status.cleanup.entity_token == agent.entity_token
    assert status.pending is not None


def test_recovery_purges_only_deleted_incarnation_and_preserves_replacement(app):
    from hyprial.daemon.impl.application.model_binding.cleanup import recover_destroyed

    old = app.agents.require("worker")
    prepare(app, old)
    store = app.desired_state.model_binding_store
    store.mark_cleanup(old.agent_id, old.entity_token, "interrupted")
    app.agents.destroy("worker", expected_entity_token=old.entity_token)
    app.handle("agent.create", {"name": "worker"})
    new = app.agents.require("worker")
    assert new.agent_id != old.agent_id
    prepare(app, new)
    recover_destroyed(app)
    assert store.status(old.agent_id, old.uri).cleanup is None
    assert store.status(old.agent_id, old.uri).operations == ()
    assert store.status(new.agent_id, new.uri).pending is not None


def test_recovery_keeps_marker_when_registry_is_unreadable(app, monkeypatch):
    from hyprial.daemon.impl.application.model_binding.cleanup import recover_destroyed

    agent = app.agents.require("worker")
    store = app.desired_state.model_binding_store
    store.mark_cleanup(agent.agent_id, agent.entity_token, "uncertain")

    def fail(_id):
        raise OSError("synthetic registry failure")

    monkeypatch.setattr(app._agent_registry, "get_by_agent_id", fail)
    recover_destroyed(app)
    assert store.status(agent.agent_id, agent.uri).cleanup is not None
    with pytest.raises(DesiredStateError, match="destroy-cleanup-pending"):
        prepare(app, agent)


def test_failed_destroy_rolls_back_own_marker_and_can_retry(app):
    agent = app.agents.require("worker")
    store = app.desired_state.model_binding_store
    store.mark_cleanup(agent.agent_id, agent.entity_token, "interrupted")

    def fail(_agent):
        raise RuntimeError("synthetic pre-effect refusal")

    with pytest.raises(RuntimeError, match="pre-effect refusal"):
        app._destroy_agent("worker", before_effect=fail)
    assert store.status(agent.agent_id, agent.uri).cleanup is None
    assert app.agents.require("worker").agent_id == agent.agent_id
    assert app._destroy_agent("worker")["destroyed"] is True


def test_preview_tracks_bindings_and_rejects_changed_configuration(app):
    preview = app.handle("agent.destroy.preview", {"name": "worker"})
    assert preview["modelBindingCleanup"] == {"bindings": 0, "operations": 0}
    agent = app.agents.require("worker")
    prepare(app, agent)
    with pytest.raises(DaemonRequestError) as caught:
        app.handle("agent.destroy", {"operationId": preview["operationId"]})
    assert caught.value.code == "PREVIEW_STALE"
    assert app.agents.require("worker").agent_id == agent.agent_id


def test_public_destroy_and_replay_preserve_cleanup_failure_data(app, monkeypatch):
    preview = app.handle("agent.destroy.preview", {"name": "worker"})

    def fail(*args, **kwargs):
        raise OSError("synthetic purge failure")

    monkeypatch.setattr(ModelBindingStore, "purge_destroyed_agent", fail)
    for _ in range(2):
        with pytest.raises(DaemonRequestError) as caught:
            app.handle("agent.destroy", {"operationId": preview["operationId"]})
        assert caught.value.code == "MODEL_CONFIGURATION_INCOMPLETE"
        assert caught.value.data == {
            "phase": "bind",
            "reason": "destroy-cleanup-pending",
            "entityDeleted": True,
        }
