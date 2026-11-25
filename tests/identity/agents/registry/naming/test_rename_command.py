"""UUID addressing §2.2: rename goes through the Agent actor command path.

The daemon mutates agents only through ``AgentActor`` commands, so a rename
must refresh the projection the read faces (and the directory facade) serve.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.composition.domains import AgentSessionDomains
from hyprial.daemon.impl.composition.events import (
    CorrelatedDomainEvents,
)
from hyprial.daemon.impl.composition.views import AgentDirectoryFacade
from hyprial.identity import AgentActor, AgentRegistry, RenameAgentCommand
from tests.support.actor_hosts import new_actor_host


@pytest.fixture
def facade(registry: AgentRegistry):
    registry.create("foo")
    events = CorrelatedDomainEvents()
    actor = AgentActor(registry, event_sink=events.publish, actor_host=new_actor_host())
    domains = object.__new__(AgentSessionDomains)
    domains.agent, domains._registry, domains._agent_events = actor, registry, events
    try:
        yield AgentDirectoryFacade(domains), actor
    finally:
        assert actor.drain(3)
        events.close()


def test_rename_command_refreshes_the_projection(facade, registry) -> None:
    port, actor = facade
    foo = registry.require("foo")
    renamed = port.rename("foo", "bar")
    assert renamed.display_name == "bar"
    assert renamed.actor == "foo"
    projected = actor.read_agent("bar")
    assert projected is not None and projected.actor == "foo"
    assert projected.display_name == "bar"
    payload = projected.to_payload()
    assert payload["agentId"] == foo.agent_id
    assert payload["displayName"] == "bar"
    assert actor.read_local_actor("bar") == "foo"
    assert port.get("bar").display == "bar"
    assert port.uri_for("bar") == foo.uri


def test_rename_command_takes_a_destroyed_agents_name(facade, registry) -> None:
    port, actor = facade
    registry.create("gone")
    registry.destroy("gone")
    assert port.rename("foo", "gone").display == "gone"
    assert actor.read_local_actor("gone") == "foo"


def test_rename_command_is_a_typed_public_command() -> None:
    command = RenameAgentCommand("c", "foo", "bar")
    assert (command.name, command.display_name) == ("foo", "bar")
