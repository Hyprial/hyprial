"""A real isolated Secret/grant composes the exact pinned Pi connection."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.identity import SecretResolver, SecretSource
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.daemon.impl.application.model_binding.admission import intent_digest, pin
from hyprial.daemon.impl.application.netendpoints.endpoints import (
    compose_daemon_worker_launch,
)
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.harnesses.pi.bindings.errors import PiModelBindingError
from .test_destroy import app as app
from tests.identity.agents.home.roots.lifecycle.support import (
    _isolated_environment as _isolated_environment,
)
from tests.daemon.cells.runtime.cell_runtime_support import authority_for


@pytest.fixture
def configured(app):
    agent = app.agents.require("worker")
    resolver = SecretResolver(app.hyprial_home, app._agent_registry)
    resolver.configure_model_connection(
        "gateway",
        field_name="key",
        value="synthetic-bound-key",
        expected_revision=0,
        connection={
            "version": 1,
            "endpoint": "https://gateway.example/v1",
            "protocol": "openai-completions",
            "profile": "openai-chat-completions/v1",
        },
    )
    metadata = resolver.model_service_metadata("gateway")["connections"]["key"]
    app._agent_registry.grant_secret(
        "worker",
        grant_id="model",
        source=SecretSource.USER_PROVIDER,
        entry_id="gateway",
        field_name="key",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )
    cell = Cell(
        agent.cell_id,
        "agent",
        authority_for("user:owner"),
        CellScope.owner(),
        CellBinding.subject(agent.agent_id),
        members=(CellMember(agent.uri, "harness"),),
        params={"cell.members": {agent.uri: {"kind": "pi", "model": "hy4-flash"}}},
    )
    app._cell_runtime = SimpleNamespace(
        lookup=lambda _id: cell, identity=SimpleNamespace(authority=cell.authority)
    )
    store = app.desired_state.model_binding_store
    candidate = {
        "authKind": "connection",
        "connectionRef": metadata["connectionRef"],
        "configRevision": metadata["configRevision"],
        "model": "hy4-flash",
        "harness": "pi",
        "mode": "headless",
        "intentDigest": intent_digest(agent.uri, "pi", "hy4-flash"),
    }
    operation = store.prepare(agent.agent_id, agent.uri, "configure", 0, candidate)
    store.finalize(agent.agent_id, agent.uri, "configure", operation.request_digest, 0)
    channel = SimpleNamespace(
        actor=agent.uri, runtime_context=None, identity_environment=lambda: {}
    )
    spec = pin(app, HarnessLaunchSpec("pi", "worker", True))
    return app, resolver, channel, spec, cell


def compose(configured, spec=None):
    app, _, channel, original, _ = configured
    return compose_daemon_worker_launch(
        registry=app._agent_registry,
        hyprial_home=app.hyprial_home,
        spec=spec or original,
        channel=channel,
        environ={},
        app=app,
    )


def test_exact_connection_and_rotation_reuse_binding_revision(configured):
    app, resolver, _, spec, _ = configured
    launch = compose(configured)
    assert launch.model_binding.key == "synthetic-bound-key"
    assert launch.model_binding.connection.endpoint == "https://gateway.example/v1"
    assert launch.model_binding.model == "hy4-flash"
    assert launch.model_binding.provider_id == spec.model_provider
    assert "synthetic-bound-key" not in repr(launch)
    resolver.write_user_provider("gateway", {"key": "synthetic-rotated-key"})
    rotated = compose(configured)
    assert rotated.model_binding.key == "synthetic-rotated-key"
    assert rotated.model_binding.snapshot() == launch.model_binding.snapshot()
    assert app.desired_state.load().model_bindings[0].binding_revision == 1


def test_revoked_grant_never_falls_back(configured):
    app, _, _, _, _ = configured
    app._agent_registry.revoke_secret_grant("worker", "model")
    with pytest.raises(PiModelBindingError) as error:
        compose(configured)
    assert error.value.code == "MODEL_AUTH_UNAVAILABLE"


def test_changed_endpoint_requires_explicit_binding_update(configured):
    _, resolver, _, _, _ = configured
    resolver.configure_model_connection(
        "gateway",
        field_name="key",
        expected_revision=1,
        connection={
            "version": 1,
            "endpoint": "https://other.example/v1",
            "protocol": "openai-completions",
            "profile": "openai-chat-completions/v1",
        },
    )
    with pytest.raises(PiModelBindingError) as error:
        compose(configured)
    assert error.value.code == "MODEL_CONFIGURATION_CONFLICT"


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "other"},
        {"model_provider": "deepseek"},
        {"headless": False},
        {"model_binding": None},
    ],
)
def test_restore_refuses_override_or_missing_binding_snapshot(configured, changes):
    spec = configured[3]
    with pytest.raises(DaemonRequestError):
        compose(configured, replace(spec, **changes))


def test_changed_cell_model_refuses_without_credential_read(configured, monkeypatch):
    import hyprial.identity as identity

    app, _, _, _, cell = configured
    app._cell_runtime.lookup = lambda _id: replace(
        cell,
        params={
            "cell.members": {cell.members[0].ref: {"kind": "pi", "model": "other"}}
        },
        revision=2,
    )

    def unexpected(**kwargs):
        pytest.fail("changed Cell must refuse before resolving the Key")

    monkeypatch.setattr(identity, "compose_worker_child_launch", unexpected)
    with pytest.raises(DaemonRequestError) as error:
        compose(configured)
    assert error.value.data == {"phase": "bind", "reason": "cell-intent-mismatch"}


def test_cell_facts_before_first_start_include_binding_and_model(
    configured, monkeypatch
):
    from hyprial.daemon.impl.composition.cells.reconcile import AppLocalMemberFacts
    from hyprial.daemon.impl.cells.reconcile.effects import resolve_member_desired_spec
    from hyprial.daemon.impl.cells.structure import parse_structure

    app, _, _, spec, cell = configured
    agent = app.agents.require("worker")
    monkeypatch.setattr("shutil.which", lambda _name: "/synthetic/pi")
    assert app.desired_state.load().harnesses == ()
    facts = AppLocalMemberFacts(app).facts_for(agent.agent_id, agent.uri)
    assert facts is not None
    assert facts.model_binding == spec.model_binding
    assert facts.model == "hy4-flash"
    desired, reason = resolve_member_desired_spec(
        agent.agent_id, parse_structure(cell).members[0], facts
    )
    assert reason is None
    assert desired.to_launch_spec("worker").model_binding == spec.model_binding
    changed = replace(facts, model_binding={**spec.model_binding, "bindingRevision": 2})
    newer, reason = resolve_member_desired_spec(
        agent.agent_id, parse_structure(cell).members[0], changed
    )
    assert reason is None
    assert newer.config_digest != desired.config_digest


def test_grant_revocation_does_not_become_cell_configuration_drift(configured, monkeypatch):
    from hyprial.daemon.impl.composition.cells.reconcile import AppLocalMemberFacts
    from hyprial.daemon.impl.cells.reconcile.effects import resolve_member_desired_spec
    from hyprial.daemon.impl.cells.structure import parse_structure

    app, _, _, _, cell = configured
    agent = app.agents.require("worker")
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/pi")
    document = parse_structure(cell).members[0]
    facts = AppLocalMemberFacts(app)
    before, reason = resolve_member_desired_spec(agent.agent_id, document,
                                                facts.facts_for(agent.agent_id, agent.uri))
    assert reason is None
    app._agent_registry.revoke_secret_grant("worker", "model")
    after, reason = resolve_member_desired_spec(agent.agent_id, document,
                                               facts.facts_for(agent.agent_id, agent.uri))
    assert reason is None and before.config_digest == after.config_digest
    with pytest.raises(PiModelBindingError, match="authorize/missing"):
        compose(configured)
