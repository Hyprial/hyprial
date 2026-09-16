"""Explicit, atomic PAC schema migrations (slice-1 databases had version 0).

Do not use executescript inside the transaction: sqlite3 would commit before
running it. In particular future node-kind CHECK changes require a migration,
not a changed CREATE TABLE IF NOT EXISTS declaration.
"""

from __future__ import annotations

import sqlite3
from time import time_ns
from uuid import uuid4

from .journal import JOURNAL_SCHEMA, append_event

SCHEMA_VERSION = 7


def _create_v1(db: sqlite3.Connection, schema: str) -> None:
    statement = ""
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""


def _upgrade_v1_to_v2(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE notifications ADD COLUMN plan_json TEXT")
    db.execute(JOURNAL_SCHEMA)
    db.execute("CREATE INDEX journal_graph_order ON journal(graph_id, seq)")
    db.execute(
        "CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal "
        "BEGIN SELECT RAISE(ABORT, 'PAC journal is append-only'); END"
    )
    db.execute(
        "CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal "
        "BEGIN SELECT RAISE(ABORT, 'PAC journal is append-only'); END"
    )
    # Old outbox rows have no historical predecessor snapshots or delivery
    # history. Preserve them verbatim, and label the imported current state
    # honestly rather than manufacture past decisions/delivery transitions.
    for graph in db.execute("SELECT * FROM graphs ORDER BY graph_id").fetchall():
        graph_id = graph["graph_id"]
        data = {"sourceSchemaVersion": 1, "graph": dict(graph)}
        for table in ("nodes", "edges", "flag_events"):
            data[table] = [dict(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE graph_id = ? ORDER BY rowid", (graph_id,)
            )]
        data["notifications"] = [dict(row) for row in db.execute(
            "SELECT n.* FROM notifications n LEFT JOIN flag_events e "
            "ON e.event_id = n.event_id WHERE e.graph_id = ? "
            "OR (e.event_id IS NULL AND substr(n.event_id, 1, ?) = ?) ORDER BY n.rowid",
            (graph_id, len(f"overdue:{graph_id}:"), f"overdue:{graph_id}:"),
        )]
        append_event(db, graph_id=graph_id, version=graph["version"],
                     type="migration_baseline", at=time_ns() // 1_000_000, data=data)


def _upgrade_v2_to_v3(db: sqlite3.Connection) -> None:
    for column in ("activated_at INTEGER", "activated_by TEXT", "closed_at INTEGER", "closed_by TEXT"):
        db.execute(f"ALTER TABLE graphs ADD COLUMN {column}")
    db.execute("CREATE TABLE journal_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), journal_id TEXT NOT NULL)")
    db.execute("INSERT INTO journal_meta VALUES (1, ?)", (str(uuid4()),))
    # Existing graphs did not have the public snapshot contract. Consumers
    # must bootstrap, not guess historical structure from their old cursors.
    for graph in db.execute("SELECT * FROM graphs ORDER BY graph_id").fetchall():
        gid = graph["graph_id"]
        has_facts = db.execute("SELECT 1 FROM flag_events WHERE graph_id=? LIMIT 1", (gid,)).fetchone()
        has_clock = db.execute(
            "SELECT 1 FROM notifications WHERE json_extract(plan_json,'$.graphId')=? "
            "OR (plan_json IS NULL AND substr(event_id,1,?)=?) LIMIT 1",
            (gid, len(f"overdue:{gid}:"), f"overdue:{gid}:"),
        ).fetchone()
        at = time_ns() // 1_000_000
        if has_facts or has_clock:
            db.execute("UPDATE graphs SET activated_at=?, activated_by='migration' WHERE graph_id=?", (at, gid))
        append_event(db, graph_id=gid, version=graph["version"], type="migration_baseline",
                     at=at, data={"sourceSchemaVersion": 2, "resync": True})


def _upgrade_v3_to_v4(db: sqlite3.Connection) -> None:
    """Add owned actor/end nodes without trusting CREATE IF NOT EXISTS.

    SQLite cannot widen a CHECK constraint in place, so copy the exact v3
    rows through a replacement table.  Actor metadata is deliberately absent
    on migrated task/clock rows rather than inferred from their owner names.
    """
    db.execute(
        """
        CREATE TABLE nodes_v4 (
            graph_id        TEXT NOT NULL,
            node_id         TEXT NOT NULL,
            owner           TEXT NOT NULL,
            brief_ref       TEXT NOT NULL,
            kind            TEXT NOT NULL DEFAULT 'task'
                            CHECK (kind IN ('task', 'clock', 'actor', 'end')),
            deadline_ms     INTEGER,
            flag            INTEGER NOT NULL DEFAULT 0,
            flag_set_by     TEXT,
            flag_set_at     INTEGER,
            flag_reason_ref TEXT,
            actor_name      TEXT UNIQUE,
            launch_ref      TEXT,
            PRIMARY KEY (graph_id, node_id),
            FOREIGN KEY (graph_id) REFERENCES graphs (graph_id),
            CHECK ((kind = 'actor' AND actor_name IS NOT NULL AND launch_ref IS NOT NULL)
                OR (kind != 'actor' AND actor_name IS NULL AND launch_ref IS NULL))
        )
        """
    )
    db.execute(
        "INSERT INTO nodes_v4 "
        "(graph_id,node_id,owner,brief_ref,kind,deadline_ms,flag,flag_set_by,flag_set_at,flag_reason_ref) "
        "SELECT graph_id,node_id,owner,brief_ref,kind,deadline_ms,flag,flag_set_by,flag_set_at,flag_reason_ref "
        "FROM nodes"
    )
    db.execute("DROP TABLE nodes")
    db.execute("ALTER TABLE nodes_v4 RENAME TO nodes")


def _upgrade_v4_to_v5(db: sqlite3.Connection) -> None:
    """Persist the current actor direction before any lifecycle side effect."""
    db.execute(
        """
        CREATE TABLE actor_activations (
            graph_id        TEXT NOT NULL,
            node_id         TEXT NOT NULL,
            incarnation     INTEGER NOT NULL DEFAULT 0 CHECK (incarnation >= 0),
            desired         TEXT NOT NULL CHECK (desired IN ('up', 'down')),
            op              TEXT NOT NULL CHECK (op IN ('pending', 'done')),
            effect_id       TEXT NOT NULL,
            operation_id    TEXT NOT NULL UNIQUE,
            identity_marker TEXT,
            daemon_epoch    TEXT,
            launch_digest   TEXT,
            launch_json     TEXT,
            updated_at      INTEGER NOT NULL,
            PRIMARY KEY (graph_id, node_id),
            FOREIGN KEY (graph_id, node_id) REFERENCES nodes (graph_id, node_id)
        )
        """
    )
    # launch_failed is a durable owner notification, not a synthetic turn.
    db.execute(
        """
        CREATE TABLE notifications_v5 (
            event_id     TEXT NOT NULL,
            edge         TEXT NOT NULL,
            kind         TEXT NOT NULL
                         CHECK (kind IN ('turn', 'withdraw', 'overdue', 'launch_failed', 'actor_alert')),
            recipient    TEXT NOT NULL,
            round_no     INTEGER,
            text         TEXT NOT NULL,
            sender       TEXT NOT NULL,
            message_id   TEXT,
            at           INTEGER NOT NULL,
            delivered_at INTEGER,
            plan_json    TEXT,
            PRIMARY KEY (event_id, edge)
        )
        """
    )
    db.execute(
        "INSERT INTO notifications_v5 SELECT event_id,edge,kind,recipient,round_no,text,sender,"
        "message_id,at,delivered_at,plan_json FROM notifications"
    )
    db.execute("DROP TABLE notifications")
    db.execute("ALTER TABLE notifications_v5 RENAME TO notifications")
    db.execute("CREATE INDEX notifications_recipient ON notifications(recipient, at)")


def _upgrade_v5_to_v6(db: sqlite3.Connection) -> None:
    # Requirements are node data.  NULL means no capability constraint; old
    # owners are not guessed into a role/tier during migration.
    db.execute("ALTER TABLE nodes ADD COLUMN requires_json TEXT")


def _upgrade_v6_to_v7(db: sqlite3.Connection) -> None:
    """Add explicit guarded clocks and caller-owned graph identity.

    Both columns are nullable: existing clocks remain unguarded and existing
    graphs keep their random graph id as their only identity.  The partial
    unique index is the durable operation-key contract for new callers.
    """

    db.execute("ALTER TABLE graphs ADD COLUMN operation_key TEXT")
    db.execute(
        "CREATE UNIQUE INDEX graphs_operation_key "
        "ON graphs(operation_key) WHERE operation_key IS NOT NULL"
    )
    db.execute("ALTER TABLE nodes ADD COLUMN guarded_by_node_id TEXT")


def migrate(db: sqlite3.Connection, legacy_schema: str) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"unsupported PAC schema version {version}")
        if version == 0:
            # The real unversioned slice-1 schema, or an empty new database.
            _create_v1(db, legacy_schema)
            db.execute("PRAGMA user_version = 1")
            version = 1
        if version == 1:
            _upgrade_v1_to_v2(db)
            db.execute("PRAGMA user_version = 2")
            version = 2
        if version == 2:
            _upgrade_v2_to_v3(db)
            db.execute("PRAGMA user_version = 3")
            version = 3
        if version == 3:
            _upgrade_v3_to_v4(db)
            db.execute("PRAGMA user_version = 4")
            version = 4
        if version == 4:
            _upgrade_v4_to_v5(db)
            db.execute("PRAGMA user_version = 5")
            version = 5
        if version == 5:
            _upgrade_v5_to_v6(db)
            db.execute("PRAGMA user_version = 6")
            version = 6
        if version == 6:
            _upgrade_v6_to_v7(db)
            db.execute("PRAGMA user_version = 7")
        db.commit()
    except BaseException:
        db.rollback()
        raise
