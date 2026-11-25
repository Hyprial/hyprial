"""Upgrade the frozen bd6eb960 database through DaemonApplication construction.

Decision 2026-11-01 (0.5.5.3 shards-green): agent_secret_grants gains the
appended owner_actor / owner_entity_token pair in this tree; legacy rows must
carry exactly NULL in both, mirroring the lifecycle_receipts fence carve-out.
Configured model service grants add route_required=0 to legacy rows; compare
the other fields exactly and reject a changed default.

The host-invite deletion (owner ruling 2026-11-21, round 2) leaves the
stored schema untouched, so this pre-host-invite fixture is still upgraded to
the three-value ``hosted_by`` CHECK exactly as before: fresh and existing
databases keep one shape, and ``'host-invite'`` remains a legal stored value
that no writer mints anymore.
SQLite's generated index names may change after a table swap, so schema facts
compare index properties rather than depending on those generated names.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from hyprial.identity import agent_id_for_entity_token
from hyprial.kernel import canonical_agent_uri, parse_agent_uri
from tests.daemon.organizations.invite.support import (
    TABLES,
    app_factory as _app_factory,
    database_rows,
    home_files,
)

# Explicit pytest fixture re-export for the migration module.
app_factory = _app_factory

FIXTURE = Path(__file__).resolve().parents[4] / 'tests/fixtures/registry-bd6eb960'


def old_database(root: Path) -> Path:
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name, key in (("database.sql", "sqlSha256"), ("home.json", "homeSha256")):
        assert (
            hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == provenance[key]
        )
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)

    def substitute(text):
        return text.replace("__HOME__", str(home)).replace("__UID__", str(os.getuid()))

    for entry in json.loads((FIXTURE / "home.json").read_text()):
        path = home / entry["path"]
        if entry["kind"] == "dir":
            path.mkdir(mode=int(entry["mode"], 8))
        else:
            path.write_text(substitute(entry["text"]))
            path.chmod(int(entry["mode"], 8))
    database = root / "state/agents.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(substitute((FIXTURE / "database.sql").read_text()))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='agents'"
        ).fetchone()[0]
        assert "'host-invite'" not in ddl
        info = {row[1]: row for row in connection.execute("PRAGMA table_info(agents)")}
        assert info["entity_token"][3] == 1
    finally:
        connection.close()
    return database


FIXTURE_AGENT_COLUMNS = (
    "actor", "owner", "machine", "uri", "entity_token", "cwd", "config",
    "provider", "model", "capabilities", "harness_args", "preferred_harness",
    "last_harness", "last_session_id", "created_at_ms", "hosted_by",
)


def agents_rows_projected(database: Path) -> list[tuple]:
    """Post-open ``agents`` rows projected onto the fixture's column list.

    The open adds only nullable/derived columns (``last_active_at_ms``,
    ``agent_id``, ``display_name`` — wherever the legacy column order leaves
    them) and re-mints ``uri`` into id form under the row's own owner and
    node; everything else must compare exactly, whatever order the ALTERs
    appended the new columns in.
    """
    connection = sqlite3.connect(database)
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(agents)")]
        rows = connection.execute("SELECT * FROM agents").fetchall()
    finally:
        connection.close()
    projected = []
    for row in rows:
        named = dict(zip(columns, row))
        assert named["last_active_at_ms"] is None
        assert named["display_name"] is None
        assert named["agent_id"] == agent_id_for_entity_token(named["entity_token"])
        owner, node, leaf = parse_agent_uri(named["uri"])
        assert leaf == named["agent_id"]
        named["uri"] = canonical_agent_uri(owner, node, named["actor"])
        projected.append(tuple(named[name] for name in FIXTURE_AGENT_COLUMNS))
    return sorted(projected)


def assert_rows_equal(before, after, database):
    # Name the lost table in a failing assertion: FK checks can pass after CASCADE.
    for table in TABLES:
        if table == "agents":
            assert agents_rows_projected(database) == before[table], (
                "persistent rows changed: agents"
            )
            continue
        if (
            table == "lifecycle_receipts"
            and before[table]
            and len(after[table][0]) == len(before[table][0]) + 1
        ):
            # The additive incarnation fence is appended to a legacy table;
            # every pre-existing row must gain only a NULL in that column.
            assert all(row[-1] is None for row in after[table])
            comparable = [row[:-1] for row in after[table]]
        elif (
            table == "agent_secret_grants"
            and before[table]
            and len(after[table][0]) == len(before[table][0]) + 4
        ):
            assert all(row[-1] == agent_id_for_entity_token(row[1]) for row in after[table])
            assert all(row[-4] is None and row[-3] is None and row[-2] == 0 for row in after[table])
            comparable = [row[:-4] for row in after[table]]
        elif (
            table == "agent_secret_grants"
            and before[table]
            and len(after[table][0]) == len(before[table][0]) + 3
        ):
            assert all(row[-1] == agent_id_for_entity_token(row[1]) for row in after[table])
            assert all(row[-3] is None and row[-2] is None for row in after[table])
            comparable = [row[:-3] for row in after[table]]
        elif (
            table == "agent_secret_grants"
            and before[table]
            and len(after[table][0]) == len(before[table][0]) + 2
        ):
            assert all(row[-2] is None and row[-1] is None for row in after[table])
            comparable = [row[:-2] for row in after[table]]
        elif table == "lifecycle_resources":
            comparable = [_without_naming_keys(row) for row in after[table]]
            assert comparable == [_without_naming_keys(row) for row in before[table]], (
                f"persistent rows changed: {table}"
            )
            continue
        else:
            comparable = after[table]
        assert comparable == before[table], f"persistent rows changed: {table}"


def _name_form_uri(row):
    """The P4 open re-mints ``agents.uri`` under the row's id, same owner and node.

    Returns the row with that URI mapped back to the pre-flip name form, so
    the rest of the row is compared exactly.
    """

    owner, node, leaf = parse_agent_uri(row[3])
    assert leaf == row[-2] == agent_id_for_entity_token(row[4])
    return (*row[:3], canonical_agent_uri(owner, node, row[0]), *row[4:])


def _without_naming_keys(row):
    """Agent-record payloads gain only the additive agentId/displayName keys."""

    parsed = _parsed(row)
    if not str(row[0]).startswith("agent-record:"):
        return parsed
    payload = dict(parsed[3])
    assert str(payload.pop("agentId", "a.")).startswith("a.")
    assert payload.pop("displayName", None) is None
    return (*parsed[:3], payload, *parsed[4:])


def _parsed(row):
    return (*row[:3], json.loads(row[3]), *row[4:])


def test_preserves_old_database_rows(tmp_path, app_factory):
    database = old_database(tmp_path)
    before = database_rows(database)
    assert all(before[table] for table in TABLES)
    files = home_files(tmp_path / "home")
    app = app_factory()
    assert_rows_equal(before, database_rows(database), database)
    assert home_files(app.hyprial_home) == files
    for actor in ("native", "hosted"):
        receipt = app._agent_registry.home_receipt(actor)
        assert receipt.entity_token == app.agents.require(actor).entity_token
    assert app._agent_registry._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert app._agent_registry._db.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(database) as connection:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='agents'"
        ).fetchone()[0]
        assert "'host-invite'" in ddl
    created = app.handle("agent.create", {"name": "newcomer"})
    assert created["ok"] is True and created["agent"]["hostedBy"] is None


def assert_sql_check(database):
    connection = sqlite3.connect(database)
    try:
        statement = "INSERT INTO agents(actor,owner,machine,uri,entity_token,created_at_ms,hosted_by) VALUES (?,?,?,?,?,?,?)"
        connection.execute(
            statement,
            (
                "raw",
                "visitor",
                "host-node",
                "agent:visitor:host-node:raw",
                "synthetic-token-raw",
                123,
                "host-invite",
            ),
        )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                statement,
                (
                    "raw",
                    "visitor",
                    "host-node",
                    "agent:visitor:host-node:raw",
                    "synthetic-token-raw",
                    123,
                    "invalid",
                ),
            )
    finally:
        connection.rollback()
        connection.close()


def schema_facts(database):
    connection = sqlite3.connect(database)
    try:
        result = {}
        for table in TABLES:
            result[table] = {
                "columns": connection.execute(f"PRAGMA table_info({table})").fetchall(),
                "foreign_keys": connection.execute(
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall(),
                "indexes": sorted(
                    (
                        row[2],
                        row[3],
                        row[4],
                        tuple(
                            connection.execute(
                                f'PRAGMA index_info("{row[1]}")'
                            ).fetchall()
                        ),
                    )
                    for row in connection.execute(
                        f"PRAGMA index_list({table})"
                    ).fetchall()
                ),
            }
        return result
    finally:
        connection.close()


def test_schema_old_and_fresh_match(tmp_path, app_factory):
    old_database(tmp_path / "old")
    old, fresh = app_factory(tmp_path / "old"), app_factory(tmp_path / "fresh")
    for app in (old, fresh):
        assert_sql_check(app._agent_registry.database)
    assert schema_facts(old._agent_registry.database) == schema_facts(
        fresh._agent_registry.database
    )


def test_schema_reopen_is_idempotent(tmp_path, app_factory):
    database = old_database(tmp_path)
    first = app_factory()
    first.handle("agent.create", {"name": "newcomer"})
    before, files = (
        {**database_rows(database), "agents": agents_rows_projected(database)},
        home_files(first.hyprial_home),
    )
    first._close()
    second = app_factory()
    assert_rows_equal(before, database_rows(database), database)
    assert home_files(second.hyprial_home) == files
    assert (
        second._agent_registry.home_receipt("newcomer").entity_token
        == second.agents.require("newcomer").entity_token
    )


def test_rollback_during_rebuild(tmp_path, app_factory, monkeypatch):
    database = old_database(tmp_path)
    before, files = database_rows(database), home_files(tmp_path / "home")
    original_connect = sqlite3.connect
    connections = []

    class FailRename(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if (
                " ".join(sql.split()).upper()
                == "ALTER TABLE AGENTS_ENTITY_MIGRATED RENAME TO AGENTS"
            ):
                raise sqlite3.OperationalError("synthetic rename I/O failure")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        if Path(args[0]) == database:
            kwargs["factory"] = FailRename
        result = original_connect(*args, **kwargs)
        connections.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        try:
            with pytest.raises(
                sqlite3.OperationalError, match="synthetic rename I/O failure"
            ):
                app_factory()
        finally:
            for connection in connections:
                connection.close()
    assert database_rows(database) == before, "failed rebuild changed rows"
    assert home_files(tmp_path / "home") == files
    connection = original_connect(database)
    try:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='agents'"
        ).fetchone()[0]
        assert "'host-invite'" not in ddl
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='agents_entity_migrated'"
            ).fetchall()
            == []
        )
    finally:
        connection.close()
    # The same database remains usable, and the next real open can finish upgrading.
    app = app_factory()
    assert app.handle("agent.create", {"name": "retry"})["created"] is True
