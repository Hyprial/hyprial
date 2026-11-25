"""UUID addressing P2: the registry resolves an agent by its immutable id.

Design: docs/design/uuid-addressing-migration-path.md §4 P2 (registry
resolves by ``agent_id`` through a unique index).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry

OWNER = "op"
NODE = "node-1"


@pytest.fixture
def registry(tmp_path: Path):
    instance = AgentRegistry(tmp_path / "agents.sqlite3", owner=OWNER, machine=NODE)
    try:
        yield instance
    finally:
        instance.close()


def test_agent_id_is_stored_under_a_unique_index(registry: AgentRegistry) -> None:
    agent = registry.create("foo")
    row = registry._db.execute(
        "SELECT agent_id FROM agents WHERE actor = ?", ("foo",)
    ).fetchone()
    assert row["agent_id"] == agent.agent_id
    indexes = {
        row["name"]: row
        for row in registry._db.execute("PRAGMA index_list(agents)")
    }
    assert int(indexes["agents_agent_id"]["unique"]) == 1


def test_get_by_agent_id_returns_the_record(registry: AgentRegistry) -> None:
    foo = registry.create("foo")
    registry.create("bar")
    assert registry.get_by_agent_id(foo.agent_id) == registry.require("foo")
    assert registry.get_by_agent_id("a." + "a" * 26) is None
    assert registry.get_by_agent_id("foo") is None


def test_local_actor_accepts_bare_id_and_id_uri(registry: AgentRegistry) -> None:
    foo = registry.create("foo")
    id_uri = f"agent:{OWNER}:{NODE}:{foo.agent_id}"
    assert registry.local_actor(foo.agent_id) == "foo"
    assert registry.local_actor(id_uri) == "foo"
    assert registry.get(id_uri) == registry.require("foo")
    assert registry.local_actor(f"agent:other:{NODE}:{foo.agent_id}") is None
    assert registry.local_actor("a." + "b" * 26) is None


def test_rotated_incarnation_gets_a_new_indexed_id(registry: AgentRegistry) -> None:
    before = registry.create("foo")
    after = registry.rotate_secret_authority("foo")
    assert after.agent_id != before.agent_id
    assert registry.get_by_agent_id(after.agent_id).actor == "foo"
    assert registry.get_by_agent_id(before.agent_id) is None


def test_existing_database_is_backfilled_on_open(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    first = AgentRegistry(database, owner=OWNER, machine=NODE)
    created = first.create("foo")
    first.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX agents_agent_id")
    connection.execute("ALTER TABLE agents DROP COLUMN agent_id")
    connection.commit()
    connection.close()

    reopened = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        assert reopened.get_by_agent_id(created.agent_id).actor == "foo"
    finally:
        reopened.close()


def test_row_written_without_id_by_an_older_binary_still_resolves(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        created = registry.create("foo")
        registry._db.execute("UPDATE agents SET agent_id = NULL WHERE actor = 'foo'")
        registry._db.commit()
        assert registry.get_by_agent_id(created.agent_id).actor == "foo"
    finally:
        registry.close()


def test_actor_key_maps_every_address_form_to_the_actor(
    registry: AgentRegistry, monkeypatch
) -> None:
    from hyprial.identity.impl.agents import addressing

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    agent = registry.create("foo")
    assert agent.uri == f"agent:{OWNER}:{NODE}:{agent.agent_id}"
    for value in ("foo", agent.agent_id, agent.uri, f"agent:{OWNER}:{NODE}:foo"):
        assert registry.actor_key(value) == "foo"
    assert registry.uri_for(agent.uri) == agent.uri


def test_session_history_accepts_the_id_address(
    registry: AgentRegistry, monkeypatch
) -> None:
    from hyprial.identity.impl.agents import addressing

    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    agent = registry.create("foo")
    registry.record_session_ref(agent.uri, agent.entity_token, "ref-1")
    rows = registry._db.execute(
        "SELECT actor FROM agent_session_ref_history WHERE session_ref = 'ref-1'"
    ).fetchall()
    assert [row["actor"] for row in rows] == ["foo"]
