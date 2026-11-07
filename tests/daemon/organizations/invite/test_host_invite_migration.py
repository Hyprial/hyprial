"""Upgrade the frozen bd6eb960 database through DaemonApplication construction.

Decision 2026-11-01 (0.5.5.3 shards-green): agent_secret_grants gains the
appended owner_actor / owner_entity_token pair in this tree; legacy rows must
carry exactly NULL in both, mirroring the lifecycle_receipts fence carve-out.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from tests.daemon.organizations.invite.test_host_invite import TABLES, app_factory as _app_factory, database_rows, home_files

# Explicit pytest fixture re-export for the migration module.
app_factory = _app_factory

FIXTURE = Path(__file__).resolve().parents[4] / 'tests/fixtures/host-invite-bd6eb960'


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


def assert_rows_equal(before, after):
    # Name the lost table in a failing assertion: FK checks can pass after CASCADE.
    for table in TABLES:
        if (
            table == "agents"
            and before[table]
            and len(after[table][0]) == len(before[table][0]) + 1
        ):
            assert all(row[14] is None for row in after[table])
            comparable = [row[:14] + row[15:] for row in after[table]]
        elif (
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
            and len(after[table][0]) == len(before[table][0]) + 2
        ):
            assert all(row[-2] is None and row[-1] is None for row in after[table])
            comparable = [row[:-2] for row in after[table]]
        else:
            comparable = after[table]
        assert comparable == before[table], f"persistent rows changed: {table}"


def test_preserves_old_database_rows(tmp_path, app_factory):
    database = old_database(tmp_path)
    before = database_rows(database)
    assert all(before[table] for table in TABLES)
    files = home_files(tmp_path / "home")
    app = app_factory()
    assert_rows_equal(before, database_rows(database))
    assert home_files(app.hyprial_home) == files
    for actor in ("native", "hosted"):
        receipt = app._agent_registry.home_receipt(actor)
        assert receipt.entity_token == app.agents.require(actor).entity_token
    assert app._agent_registry._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert app._agent_registry._db.execute("PRAGMA foreign_key_check").fetchall() == []
    result = app.handle(
        "agent.host-invite", {"name": "new-visitor", "owner": "new-owner"}
    )
    assert result["ok"] is True and result["agent"]["hostedBy"] == "host-invite"


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
                # SQLite's generated index names can change after a table swap.
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
    first.handle("agent.host-invite", {"name": "new-visitor", "owner": "new-owner"})
    before, files, schema = (
        database_rows(database),
        home_files(first.hyprial_home),
        schema_facts(database),
    )
    first._close()
    second = app_factory()
    assert_rows_equal(before, database_rows(database))
    assert home_files(second.hyprial_home) == files
    assert schema_facts(database) == schema
    assert (
        second._agent_registry.home_receipt("new-visitor").entity_token
        == second.agents.require("new-visitor").entity_token
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
    assert_rows_equal(before, database_rows(database))
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
    assert (
        app.handle("agent.host-invite", {"name": "retry", "owner": "visitor"})[
            "created"
        ]
        is True
    )
