# Decision record (S3a red tests R4; no inline comments by repository rule):
# - squire-container leaves the hosting authorities.  The Agent entity refuses
#   it, a fresh database refuses it in SQL, and a database created with the
#   old three-value CHECK that still holds such a row makes the registry fail
#   loud naming the row.  The repair the migration guide prints
#   (UPDATE agents SET hosted_by = NULL WHERE hosted_by = 'squire-container')
#   lets the same database load and keeps the other rows (precedent: the I7b-2
#   test for the leftover user-proxy row).
# - OLD_TABLE has the current NOT NULL entity_token and the host-invite
#   CHECK, so opening it does not rebuild the table and the entity refuses the
#   row.  A table whose CHECK predates host-invite (or whose token is
#   nullable) is rebuilt on open; the rebuild refuses the row in SQL, rolls
#   back and leaves the file intact, and the same
#   repair then lets it load.  The guide runs the repair before the first
#   start of the new version.
# - host-invite stays valid on purpose; test_legacy_hosted_rows covers it.
"""A leftover squire-container row is refused and the guide's repair clears it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import Agent, AgentError, AgentRegistry

from ..support import NODE, OWNER

REPAIR = "UPDATE agents SET hosted_by = NULL WHERE hosted_by = 'squire-container';"
OLD_TABLE = (
    "CREATE TABLE agents ("
    "actor TEXT PRIMARY KEY, owner TEXT NOT NULL, machine TEXT NOT NULL, "
    "uri TEXT NOT NULL UNIQUE, entity_token TEXT NOT NULL UNIQUE, cwd TEXT, config TEXT, "
    "provider TEXT, model TEXT, capabilities TEXT NOT NULL DEFAULT '{}', "
    "harness_args TEXT NOT NULL DEFAULT '{}', preferred_harness TEXT, "
    "last_harness TEXT, last_session_id TEXT, last_active_at_ms INTEGER, "
    "created_at_ms INTEGER NOT NULL, hosted_by TEXT DEFAULT NULL "
    "CHECK (hosted_by IN ('transfer-receive', 'squire-container', 'host-invite')))"
)


def _old_database(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(OLD_TABLE)
        connection.execute(
            "INSERT INTO agents "
            "(actor, owner, machine, uri, entity_token, capabilities, "
            "harness_args, created_at_ms, hosted_by) VALUES "
            "('boxed', ?, ?, ?, 'token-1', '{}', '{}', 1, 'squire-container'), "
            "('keeper', ?, ?, ?, 'token-2', '{}', '{}', 2, NULL)",
            (OWNER, NODE, f"agent:{OWNER}:{NODE}:boxed",
             OWNER, NODE, f"agent:{OWNER}:{NODE}:keeper"),
        )


def test_the_entity_refuses_the_retired_hosting_authority() -> None:
    with pytest.raises(AgentError):
        Agent(
            uri=f"agent:{OWNER}:{NODE}:boxed",
            actor="boxed",
            owner=OWNER,
            machine=NODE,
            entity_token="token-1",
            hosted_by="squire-container",
        )


def test_a_fresh_database_refuses_the_value_in_sql(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    registry.close()

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO agents "
                "(actor, owner, machine, uri, entity_token, capabilities, "
                "harness_args, created_at_ms, hosted_by) VALUES "
                "('boxed', ?, ?, ?, 'token-1', '{}', '{}', 1, 'squire-container')",
                (OWNER, NODE, f"agent:{OWNER}:{NODE}:boxed"),
            )


def test_an_old_database_with_the_row_fails_loud_naming_it(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    _old_database(database)

    with pytest.raises(AgentError) as raised:
        registry = AgentRegistry(database, owner=OWNER, machine=NODE)
        try:
            registry.list()
        finally:
            registry.close()

    assert "boxed" in str(raised.value) or "squire-container" in str(raised.value)


def test_the_guide_repair_lets_the_database_load(tmp_path: Path) -> None:
    database = tmp_path / "agents.sqlite3"
    _old_database(database)
    with pytest.raises(AgentError):
        registry = AgentRegistry(database, owner=OWNER, machine=NODE)
        try:
            registry.list()
        finally:
            registry.close()

    with sqlite3.connect(database) as connection:
        connection.execute(REPAIR)

    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        loaded = {agent.actor: agent.hosted_by for agent in registry.list()}
    finally:
        registry.close()
    assert loaded == {"boxed": None, "keeper": None}


PRE_HOST_INVITE_TABLE = OLD_TABLE.replace(
    "('transfer-receive', 'squire-container', 'host-invite')",
    "('transfer-receive', 'squire-container')",
)


def test_a_rebuilt_database_refuses_the_row_intact_and_the_repair_loads(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(PRE_HOST_INVITE_TABLE)
        connection.execute(
            "INSERT INTO agents "
            "(actor, owner, machine, uri, entity_token, capabilities, "
            "harness_args, created_at_ms, hosted_by) VALUES "
            "('boxed', ?, ?, ?, 'token-1', '{}', '{}', 1, 'squire-container'), "
            "('keeper', ?, ?, ?, 'token-2', '{}', '{}', 2, NULL)",
            (OWNER, NODE, f"agent:{OWNER}:{NODE}:boxed",
             OWNER, NODE, f"agent:{OWNER}:{NODE}:keeper"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        AgentRegistry(database, owner=OWNER, machine=NODE)

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT actor, hosted_by FROM agents ORDER BY actor"
        ).fetchall()
        assert rows == [("boxed", "squire-container"), ("keeper", None)]
        connection.execute(REPAIR)

    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        loaded = {agent.actor: agent.hosted_by for agent in registry.list()}
    finally:
        registry.close()
    assert loaded == {"boxed": None, "keeper": None}
