"""A stale grant request must not attach to a new authority for the same actor."""

import threading

import pytest

from hyprial.identity import AgentRegistry, SecretSource
from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.identity.impl.agents.actor.ports import GrantAgentSecretCommand
from hyprial.identity.impl.agents.registry.config_binding import AgentIncarnationMismatch
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from tests.support.actor_hosts import new_actor_host


@pytest.fixture
def registry(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(tmp_path / "state" / "agents.sqlite3", owner="owner", machine="node", hyprial_home=home)
    registry.create("worker")
    try:
        yield registry
    finally:
        registry.close()


def grant(registry, **kwargs):
    return registry.grant_secret("worker", grant_id="model", source=SecretSource.USER_PROVIDER,
        entry_id="synthetic-entry", field_name="key", environment_names=("MODEL_KEY",), **kwargs)


def test_stale_target_cannot_replace_current_grant(registry):
    token = registry.require("worker").entity_token
    original = grant(registry, revision=1)
    with pytest.raises(AgentIncarnationMismatch):
        grant(registry, revision=2, target_entity_token="another-incarnation")
    assert registry.secret_grant("worker", "model") == original
    assert grant(registry, revision=2, target_entity_token=token).revision == 2


def test_transfer_refuses_old_token_without_new_grant(registry):
    old = registry.require("worker").entity_token
    registry.rotate_secret_authority("worker")
    with pytest.raises(AgentIncarnationMismatch):
        grant(registry, revision=1, target_entity_token=old)
    assert registry.secret_inventory("worker") == ()
    current = registry.require("worker").entity_token
    assert grant(registry, revision=1, target_entity_token=current).entity_token == current


def test_actor_command_preserves_target_fence(registry):
    settled = threading.Event()
    events = []

    def receive(event):
        if getattr(event, "correlation_id", None) == "stale-target":
            events.append(event)
            settled.set()

    actor = AgentActor(registry, event_sink=receive, actor_host=new_actor_host())
    try:
        command = GrantAgentSecretCommand("stale-target", registry.uri_for("worker"), "model",
            SecretSource.USER_PROVIDER, "synthetic-entry", "key", ("MODEL_KEY",), 1,
            target_entity_token="another-incarnation")
        assert actor.submit(command) is PortAdmission.ACCEPTED
        assert settled.wait(3), "grant command did not settle"
        assert isinstance(events[0], PortCommandRejected)
        assert events[0].code == "AGENT_INCARNATION_MISMATCH"
        assert registry.secret_inventory("worker") == ()
    finally:
        assert actor.drain(1).complete


@pytest.mark.parametrize("field", ["uri", "created"])
def test_read_effect_rejects_wrong_expected_instance_even_with_grants(registry, field):
    current = registry.require("worker")
    original = grant(registry, revision=1)
    actor = AgentActor(registry, event_sink=lambda event: None, actor_host=new_actor_host())
    try:
        expected = {"expected_uri": str(current.uri), "expected_created_at_ms": current.created_at_ms}
        assert actor.read_secret_inventory("worker", **expected) == (original,)
        if field == "uri":
            expected["expected_uri"] = "agent:owner:node:another"
        else:
            expected["expected_created_at_ms"] = current.created_at_ms + 1
        with pytest.raises(AgentIncarnationMismatch):
            actor.read_secret_inventory("worker", **expected)
        assert registry.secret_inventory("worker") == (original,)
    finally:
        assert actor.drain(1).complete
