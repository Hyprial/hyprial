"""UUID addressing §2.2: display names and rename (ruling U2).

``agents.actor`` stays the immutable internal key; ``display_name`` is the
renameable label.  NULL means the display equals the actor.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import (
    Agent,
    AgentExistsError,
    AgentRegistry,
    InvalidAgentNameError,
)

from .conftest import NODE, OWNER


def test_new_record_has_no_display_name_and_publishes_its_id(
    registry: AgentRegistry,
) -> None:
    agent = registry.create("foo")
    assert agent.display_name is None
    assert agent.display == "foo"
    payload = agent.to_json()
    assert payload["agentId"] == agent.agent_id
    assert payload["displayName"] is None
    assert Agent.from_json(payload, "round-trip") == agent


def test_rename_changes_only_the_display(registry: AgentRegistry) -> None:
    before = registry.create("foo")
    after = registry.rename("foo", "bar")
    assert after.display_name == "bar"
    assert after.display == "bar"
    assert (after.actor, after.uri, after.entity_token, after.created_at_ms) == (
        before.actor, before.uri, before.entity_token, before.created_at_ms,
    )
    assert registry.require("foo") == after
    assert registry.require("foo").to_json()["displayName"] == "bar"


def test_bare_display_name_resolves_to_the_immutable_actor(
    registry: AgentRegistry,
) -> None:
    foo = registry.create("foo")
    registry.rename("foo", "bar")
    assert registry.local_actor("bar") == "foo"
    assert registry.get("bar").actor == "foo"
    assert registry.uri_for("bar") == foo.uri
    assert registry.local_actor(f"agent:{OWNER}:{NODE}:bar") == "foo"
    assert registry.local_actor("baz") == "baz"


def test_rename_by_agent_id_and_back_to_the_actor(registry: AgentRegistry) -> None:
    foo = registry.create("foo")
    registry.rename(foo.agent_id, "bar")
    restored = registry.rename("bar", "foo")
    assert restored.display_name is None
    assert restored.display == "foo"


@pytest.mark.parametrize(
    "name",
    ["", "bad name", "-dash", "_turn-hooks", "a." + "b" * 26, "agent:op:node-1:x"],
)
def test_rename_applies_the_native_name_rules(
    registry: AgentRegistry, name: str
) -> None:
    registry.create("foo")
    with pytest.raises(InvalidAgentNameError):
        registry.rename("foo", name)


def test_display_names_are_unique_with_actors(registry: AgentRegistry) -> None:
    registry.create("foo")
    registry.create("other")
    with pytest.raises(AgentExistsError):
        registry.rename("foo", "other")
    registry.rename("other", "taken")
    with pytest.raises(AgentExistsError):
        registry.rename("foo", "taken")
    with pytest.raises(AgentExistsError):
        registry.create("taken")


def test_a_previous_display_name_is_free_for_another_agent(
    registry: AgentRegistry,
) -> None:
    foo = registry.create("foo")
    registry.rename("foo", "bar")
    registry.rename("foo", "baz")
    other = registry.create("bar")
    assert other.agent_id != foo.agent_id


def test_rename_takes_the_name_of_a_destroyed_agent(
    registry: AgentRegistry,
) -> None:
    registry.create("gone")
    registry.destroy("gone")
    registry.create("foo")
    assert registry.rename("foo", "gone").display == "gone"


def test_display_column_is_added_to_an_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    first = AgentRegistry(database, owner=OWNER, machine=NODE)
    first.create("foo")
    first.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX IF EXISTS agents_display_name")
    connection.execute("ALTER TABLE agents DROP COLUMN display_name")
    connection.commit()
    connection.close()
    reopened = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        assert reopened.rename("foo", "bar").display == "bar"
    finally:
        reopened.close()


@pytest.mark.parametrize("home", [False, True])
def test_create_rechecks_the_name_inside_its_insert_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: bool,
) -> None:
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3", owner=OWNER, machine=NODE,
        hyprial_home=(tmp_path / "hyprial") if home else None,
    )
    try:
        registry.create("other")
        cleanup = registry.cleanup_revoked_home

        def rename_in_between(actor: str):
            registry.rename("other", "x")
            return cleanup(actor)

        monkeypatch.setattr(registry, "cleanup_revoked_home", rename_in_between)
        with pytest.raises(AgentExistsError):
            registry.create("x")
        assert registry.get("x").actor == "other"
        assert [agent.actor for agent in registry.list()] == ["other"]
    finally:
        registry.close()


def test_home_create_rechecks_the_name_at_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3", owner=OWNER, machine=NODE,
        hyprial_home=tmp_path / "hyprial",
    )
    try:
        registry.create("other")
        provisioner = registry.home_provisioner
        provision = provisioner.provision_claimed

        def rename_in_between(receipt):
            registry.rename("other", "x")
            return provision(receipt)

        monkeypatch.setattr(provisioner, "provision_claimed", rename_in_between)
        with pytest.raises(AgentExistsError):
            registry.create("x")
        assert [agent.actor for agent in registry.list()] == ["other"]
    finally:
        registry.close()
