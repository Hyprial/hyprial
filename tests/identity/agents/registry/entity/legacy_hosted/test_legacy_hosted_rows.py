"""agent_entity: legacy host-invite registry rows stay loadable and destroyable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hyprial.identity.impl.agents import AgentRegistry

from ..support import NODE, OWNER


def test_host_invite_rows_survive_nullable_token_rebuild_and_destroy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE agents ("
            "actor TEXT PRIMARY KEY, owner TEXT NOT NULL, machine TEXT NOT NULL, "
            "uri TEXT NOT NULL UNIQUE, entity_token TEXT, cwd TEXT, config TEXT, "
            "provider TEXT, model TEXT, capabilities TEXT NOT NULL DEFAULT '{}', "
            "harness_args TEXT NOT NULL DEFAULT '{}', preferred_harness TEXT, "
            "last_harness TEXT, last_session_id TEXT, last_active_at_ms INTEGER, "
            "created_at_ms INTEGER NOT NULL, hosted_by TEXT DEFAULT NULL "
            "CHECK (hosted_by IN ('transfer-receive', 'squire-container', 'host-invite')))"
        )
        connection.execute(
            "INSERT INTO agents "
            "(actor, owner, machine, uri, entity_token, capabilities, "
            "harness_args, created_at_ms, hosted_by) VALUES "
            "('legacy', ?, ?, ?, NULL, '{}', '{}', 1, 'host-invite'), "
            "('destroy-me', ?, ?, ?, 'stable-token', '{}', '{}', 2, NULL)",
            (
                OWNER,
                NODE,
                f"agent:{OWNER}:{NODE}:legacy",
                OWNER,
                NODE,
                f"agent:{OWNER}:{NODE}:destroy-me",
            ),
        )
        connection.execute(
            "CREATE TABLE agent_grant_journal ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL, "
            "actor TEXT NOT NULL, entity_token TEXT NOT NULL, "
            "action TEXT NOT NULL CHECK (action IN ('host-invite','grant','revoke')), "
            "grant_id TEXT NOT NULL, capability TEXT, scope TEXT, "
            '"by" TEXT NOT NULL, revision INTEGER, note TEXT)'
        )
        connection.execute(
            "INSERT INTO agent_grant_journal "
            "(at_ms, actor, entity_token, action, grant_id, \"by\") "
            "VALUES (1, 'legacy', 'legacy-token', 'host-invite', 'invite', 'owner')"
        )

    registry = AgentRegistry(database, owner=OWNER, machine=NODE)
    try:
        legacy = registry.require("legacy")
        assert legacy.hosted_by == "host-invite"
        assert legacy.entity_token
        assert {agent.actor for agent in registry.list()} == {"legacy", "destroy-me"}
        assert registry.destroy("legacy") is True
        assert registry.get("legacy") is None
        journal = registry._db.execute(  # noqa: SLF001 - inspect legacy journal
            "SELECT action FROM agent_grant_journal WHERE actor = 'legacy'"
        ).fetchone()
        assert journal["action"] == "host-invite"
        assert registry.destroy("destroy-me") is True
        assert registry.get("destroy-me") is None
    finally:
        registry.close()
