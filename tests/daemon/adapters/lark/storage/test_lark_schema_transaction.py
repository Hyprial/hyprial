"""Lark schema DDL acquires the write lock once and cleans up failed opens."""

import sqlite3

import pytest

from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore


def test_schema_ddl_is_one_immediate_transaction(tmp_path, monkeypatch):
    connect = sqlite3.connect
    observations = []
    statements = []

    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def trace(sql):
            statements.append(sql.strip())
            if sql.lstrip().startswith("CREATE") or "CREATE TABLE IF NOT EXISTS" in sql:
                observations.append(db.in_transaction)

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    store = LarkStateStore(tmp_path / "state.sqlite3")
    try:
        assert observations and all(observations), (
            "schema was built in autocommit fragments"
        )
        first_create = next(
            i for i, sql in enumerate(statements) if "CREATE TABLE" in sql
        )
        assert statements[first_create - 1] == "BEGIN IMMEDIATE;"
        assert store.pending_command_capacity().count == 0
    finally:
        store.close()


def test_failed_schema_rolls_back_and_closes_pinned_connection(tmp_path, monkeypatch):
    connect = sqlite3.connect
    connections = []
    path = tmp_path / "state.sqlite3"

    def denied_connect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, name, *unused):
            if action == sqlite3.SQLITE_CREATE_TABLE and name == "dead_letters":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", denied_connect)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized") as caught:
        LarkStateStore(path)
    with connect(path) as observer:
        assert (
            observer.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            == []
        )
    observer.close()
    for db in connections:
        try:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                db.execute("SELECT 1")
        finally:
            db.close()
    assert caught.value.__traceback__ is not None  # failing constructor is still pinned
