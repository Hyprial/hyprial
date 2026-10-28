"""PAC schema migrations 14 through 19 and the :func:`migrate` entry.

The v1-v13 upgrade catalogue lives in ``_upgrades``; this module owns the
latest steps and the version walk.  Keep the transaction atomic.
"""

from __future__ import annotations

import json
from hashlib import sha256
import sqlite3
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.pac.storage.migrations.upgrades import (
    SCHEMA_VERSION,
    _create_v1,
    _upgrade_v10_to_v11,
    _upgrade_v11_to_v12,
    _upgrade_v12_to_v13,
    _upgrade_v13_to_v14,
    _upgrade_v1_to_v2,
    _upgrade_v2_to_v3,
    _upgrade_v3_to_v4,
    _upgrade_v4_to_v5,
    _upgrade_v5_to_v6,
    _upgrade_v6_to_v7,
    _upgrade_v7_to_v8,
    _upgrade_v8_to_v9,
    _upgrade_v9_to_v10,
    unrewritten_owners_note,  # noqa: F401  (facade re-export)
)
from hyprial.daemon.impl.pac.storage.migrations.expansion import _upgrade_v17_to_v18

__all__ = [
    "SCHEMA_VERSION",
    "_upgrade_v17_to_v18",
    "migrate",
    "unrewritten_owners_note",
]


def _upgrade_v14_to_v15(db: sqlite3.Connection) -> None:
    """Add workflow-owned worker receipts and the immutable roster projection.

    A historical managed graph is backfilled only when its specification and
    actor mapping are still sufficient to reproduce the projection.  Missing
    or changed artifacts leave the new fields NULL and create no receipt;
    migration never invents ownership history from an actor-name prefix.
    """

    from hyprial.kernel import parse_agent_uri

    from hyprial.daemon.impl.pac.contracts.roster  import build_roster

    db.execute("ALTER TABLE workflow_graphs ADD COLUMN roster_json TEXT")
    db.execute("ALTER TABLE workflow_graphs ADD COLUMN roster_digest TEXT")
    db.execute(
        """
        CREATE TABLE workflow_worker_receipts (
            graph_id          TEXT NOT NULL,
            actor_node        TEXT NOT NULL,
            actor_name        TEXT NOT NULL,
            actor_uri         TEXT NOT NULL,
            ownership         TEXT NOT NULL CHECK(ownership = 'workflow'),
            launch_digest     TEXT NOT NULL,
            roster_digest     TEXT NOT NULL,
            state             TEXT NOT NULL CHECK(state = 'planned'),
            admission_generation INTEGER NOT NULL DEFAULT 0
                CHECK(admission_generation >= 0),
            agent_entity_token TEXT,
            created_at        INTEGER NOT NULL,
            PRIMARY KEY(graph_id, actor_node),
            FOREIGN KEY(graph_id, actor_node)
                REFERENCES nodes(graph_id, node_id)
        )
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX workflow_worker_receipts_actor "
        "ON workflow_worker_receipts(graph_id, actor_name)"
    )
    db.execute(
        """
        CREATE TRIGGER workflow_worker_receipt_identity_immutable
        BEFORE UPDATE OF graph_id,actor_node,actor_name,actor_uri,ownership,
                         launch_digest,roster_digest,created_at
        ON workflow_worker_receipts
        WHEN NEW.graph_id IS NOT OLD.graph_id
          OR NEW.actor_node IS NOT OLD.actor_node
          OR NEW.actor_name IS NOT OLD.actor_name
          OR NEW.actor_uri IS NOT OLD.actor_uri
          OR NEW.ownership IS NOT OLD.ownership
          OR NEW.launch_digest IS NOT OLD.launch_digest
          OR NEW.roster_digest IS NOT OLD.roster_digest
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'workflow worker receipt identity is immutable');
        END
        """
    )

    for graph in db.execute(
        "SELECT g.graph_id,g.created_at,w.specification_ref,w.specification_digest "
        "FROM graphs g JOIN workflow_graphs w USING(graph_id) ORDER BY g.graph_id"
    ).fetchall():
        try:
            path = Path(str(graph["specification_ref"]))
            if path.is_symlink():
                continue
            payload = path.read_bytes()
            if sha256(payload).hexdigest() != graph["specification_digest"]:
                continue
            specification = json.loads(payload)
            specifications = specification.get("nodes")
            if not isinstance(specifications, list):
                continue
            assignments = db.execute(
                "SELECT w.node_id,w.actor_node,n.owner,a.actor_name,a.launch_ref "
                "FROM workflow_nodes w "
                "JOIN nodes n ON n.graph_id=w.graph_id AND n.node_id=w.node_id "
                "JOIN nodes a ON a.graph_id=w.graph_id AND a.node_id=w.actor_node "
                "WHERE w.graph_id=? AND w.actor_node IS NOT NULL "
                "ORDER BY w.node_id",
                (graph["graph_id"],),
            ).fetchall()
            owned_bindings = {
                str(row["node_id"]): (str(row["actor_node"]), str(row["owner"]))
                for row in assignments
            }
            if any(
                (node.get("ownership") == "workflow")
                != (str(node.get("id")) in owned_bindings)
                for node in specifications
                if isinstance(node, dict)
            ):
                continue
            local_identity = next(
                (
                    parsed[:2]
                    for row in assignments
                    if (parsed := parse_agent_uri(str(row["owner"]))) is not None
                ),
                (None, None),
            )
            _, roster_digest, roster_json = build_roster(
                str(graph["graph_id"]),
                specifications,
                owned_bindings=owned_bindings,
                local_owner=local_identity[0],
                local_machine=local_identity[1],
            )
            actor_rows = db.execute(
                "SELECT node_id,actor_name,launch_ref FROM nodes "
                "WHERE graph_id=? AND kind='actor' ORDER BY node_id",
                (graph["graph_id"],),
            ).fetchall()
            receipt_values: list[tuple[Any, ...]] = []
            derivable = True
            for actor in actor_rows:
                assigned = [
                    row
                    for row in assignments
                    if row["actor_node"] == actor["node_id"]
                ]
                owners = {str(row["owner"]) for row in assigned}
                launch_ref = str(actor["launch_ref"] or "")
                fragment = launch_ref.partition("#sha256=")[2]
                if len(fragment) != 64 or any(
                    character not in "0123456789abcdef" for character in fragment
                ):
                    derivable = False
                    break
                if len(owners) != 1:
                    derivable = False
                    break
                actor_uri = next(iter(owners))
                parsed = parse_agent_uri(actor_uri)
                if parsed is None or parsed[2] != actor["actor_name"]:
                    derivable = False
                    break
                receipt_values.append(
                    (
                        graph["graph_id"],
                        actor["node_id"],
                        actor["actor_name"],
                        actor_uri,
                        fragment,
                        roster_digest,
                        graph["created_at"],
                    )
                )
            if not derivable:
                continue
            db.execute(
                "UPDATE workflow_graphs SET roster_json=?,roster_digest=? "
                "WHERE graph_id=?",
                (roster_json, roster_digest, graph["graph_id"]),
            )
            for values in receipt_values:
                db.execute(
                    "INSERT INTO workflow_worker_receipts "
                    "(graph_id,actor_node,actor_name,actor_uri,ownership,launch_digest,"
                    "roster_digest,state,admission_generation,created_at) "
                    "VALUES (?,?,?,?,'workflow',?,?,'planned',0,?)",
                    values,
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue


def _upgrade_v15_to_v16(db: sqlite3.Connection) -> None:
    """Persist terminal cleanup work without inferring ownership from names."""

    db.execute("ALTER TABLE workflow_worker_receipts RENAME TO workflow_worker_receipts_v15")
    db.execute(
        """
        CREATE TABLE workflow_worker_receipts (
            graph_id          TEXT NOT NULL,
            actor_node        TEXT NOT NULL,
            actor_name        TEXT NOT NULL,
            actor_uri         TEXT NOT NULL,
            ownership         TEXT NOT NULL CHECK(ownership = 'workflow'),
            launch_digest     TEXT NOT NULL,
            roster_digest     TEXT NOT NULL,
            state             TEXT NOT NULL CHECK(state IN ('planned','down')),
            admission_generation INTEGER NOT NULL DEFAULT 0
                CHECK(admission_generation >= 0),
            agent_entity_token TEXT,
            created_at        INTEGER NOT NULL,
            PRIMARY KEY(graph_id, actor_node),
            FOREIGN KEY(graph_id, actor_node)
                REFERENCES nodes(graph_id, node_id)
        )
        """
    )
    db.execute(
        "INSERT INTO workflow_worker_receipts "
        "SELECT * FROM workflow_worker_receipts_v15"
    )
    db.execute("DROP TABLE workflow_worker_receipts_v15")
    db.execute(
        "CREATE UNIQUE INDEX workflow_worker_receipts_actor "
        "ON workflow_worker_receipts(graph_id, actor_name)"
    )
    db.execute(
        """
        CREATE TRIGGER workflow_worker_receipt_identity_immutable
        BEFORE UPDATE OF graph_id,actor_node,actor_name,actor_uri,ownership,
                         launch_digest,roster_digest,created_at
        ON workflow_worker_receipts
        WHEN NEW.graph_id IS NOT OLD.graph_id
          OR NEW.actor_node IS NOT OLD.actor_node
          OR NEW.actor_name IS NOT OLD.actor_name
          OR NEW.actor_uri IS NOT OLD.actor_uri
          OR NEW.ownership IS NOT OLD.ownership
          OR NEW.launch_digest IS NOT OLD.launch_digest
          OR NEW.roster_digest IS NOT OLD.roster_digest
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'workflow worker receipt identity is immutable');
        END
        """
    )
    db.execute(
        """
        CREATE TABLE workflow_worker_cleanup_intents (
            graph_id          TEXT NOT NULL,
            actor_node        TEXT NOT NULL,
            actor_name        TEXT NOT NULL,
            actor_uri         TEXT NOT NULL,
            operation_id      TEXT NOT NULL,
            state             TEXT NOT NULL
                              CHECK(state IN ('pending','complete','attention')),
            attempts          INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            created_at        INTEGER NOT NULL,
            updated_at        INTEGER NOT NULL,
            attention_reason  TEXT,
            last_observation_json TEXT,
            PRIMARY KEY(graph_id, actor_node),
            UNIQUE(graph_id, operation_id),
            FOREIGN KEY(graph_id, actor_node)
                REFERENCES workflow_worker_receipts(graph_id, actor_node),
            CHECK ((state = 'attention') = (attention_reason IS NOT NULL))
        )
        """
    )
    db.execute(
        """
        CREATE TRIGGER workflow_worker_cleanup_identity_immutable
        BEFORE UPDATE OF graph_id,actor_node,actor_name,actor_uri,operation_id,created_at
        ON workflow_worker_cleanup_intents
        WHEN NEW.graph_id IS NOT OLD.graph_id
          OR NEW.actor_node IS NOT OLD.actor_node
          OR NEW.actor_name IS NOT OLD.actor_name
          OR NEW.actor_uri IS NOT OLD.actor_uri
          OR NEW.operation_id IS NOT OLD.operation_id
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'workflow worker cleanup identity is immutable');
        END
        """
    )


def _upgrade_v16_to_v17(db: sqlite3.Connection) -> None:
    """Give notification delivery a durable terminal-failure state.

    Existing rows remain pending: the migration cannot infer whether a prior
    attempt was transient or permanent.  Their next real attempt therefore
    either records delivery or classifies a definitive failure.
    """

    db.execute("ALTER TABLE notifications ADD COLUMN failed_at INTEGER")
    db.execute("ALTER TABLE notifications ADD COLUMN failure_code TEXT")
    db.execute("ALTER TABLE notifications ADD COLUMN failure_detail TEXT")
    db.execute(
        "CREATE INDEX notifications_graph_retryable "
        "ON notifications(json_extract(plan_json,'$.graphId'),message_id,failed_at)"
    )



def _invalidate(targets: tuple[str, ...]) -> str:
    values = ','.join(targets)
    return f"""
        INSERT INTO workflow_tick_projection(graph_id,revision)
        SELECT graph_id,1 FROM workflow_graphs
        WHERE graph_id IN ({values}) OR graph_id IN (
            SELECT parent_graph_id FROM workflow_expansions
            WHERE child_graph_id IN ({values})
        )
        ON CONFLICT(graph_id) DO UPDATE SET revision=revision+1;
    """


def _track(db: sqlite3.Connection, table: str, keys: tuple[str, ...]) -> None:
    # The table/column names come only from this migration's schema. Tracking
    # every column also covers journal-less writes such as worker restart.
    # Later migrations adding projector inputs must rebuild the matching
    # tracking triggers together with their new schema.
    columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    changed = ' OR '.join(f'OLD."{name}" IS NOT NEW."{name}"' for name in columns)
    for operation, sources in (
        ('INSERT', ('NEW',)), ('DELETE', ('OLD',)), ('UPDATE', ('OLD', 'NEW')),
    ):
        targets = tuple(f'{source}."{key}"' for source in sources for key in keys)
        guard = f'WHEN {changed}' if operation == 'UPDATE' else ''
        db.execute(f"""
            CREATE TRIGGER workflow_tick_{table}_{operation.lower()}
            AFTER {operation} ON "{table}" {guard}
            BEGIN {_invalidate(targets)} END
        """)


def upgrade_v18_to_v19(db: sqlite3.Connection) -> None:
    """Add a derived revision/deadline index; business facts stay authoritative."""

    db.execute("""
        CREATE TABLE workflow_tick_projection (
            graph_id TEXT PRIMARY KEY REFERENCES workflow_graphs(graph_id) ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 0,
            projected_revision INTEGER NOT NULL DEFAULT -1,
            next_due_ms INTEGER
        )
    """)
    db.execute('INSERT INTO workflow_tick_projection(graph_id,revision) SELECT graph_id,1 FROM workflow_graphs')
    for table in (
        'graphs', 'nodes', 'edges', 'workflow_graphs', 'workflow_nodes',
        'flag_events', 'workflow_worktrees', 'workflow_worker_receipts',
    ):
        _track(db, table, ('graph_id',))
    _track(db, 'workflow_expansions', ('parent_graph_id', 'child_graph_id'))
    # These envelopes describe facts already tracked in their business tables.
    # The projector reads actor_lost/actor_unowned/launch_failed from the
    # append-only journal itself; unknown future kinds also invalidate safely.
    db.execute(f"""
        CREATE TRIGGER workflow_tick_journal_insert AFTER INSERT ON journal
        WHEN NEW.type NOT IN (
            'flag_set','flag_reset','notification_planned','delivery_changed',
            'structure_changed','graph_activated','graph_closed','actor_up',
            'actor_down','actor_restored','migration_baseline','workflow_changed'
        )
        BEGIN {_invalidate(('NEW.graph_id',))} END
    """)
    db.execute("""
        CREATE INDEX graphs_workflow_tick_open
        ON graphs(created_at,graph_id) WHERE closed_at IS NULL
    """)
    db.execute("""
        CREATE INDEX workflow_tick_unfinished ON workflow_graphs(graph_id)
        WHERE state NOT IN ('completed','failed','cancelled')
    """)
    db.execute("""
        CREATE INDEX notifications_tick_alert
        ON notifications(json_extract(plan_json,'$.graphId'))
        WHERE message_id IS NULL AND failed_at IS NULL AND kind='actor_alert'
    """)
    db.execute("""
        CREATE INDEX notifications_tick_pending ON notifications(event_id,edge)
        WHERE message_id IS NULL AND failed_at IS NULL
    """)


def migrate(db: sqlite3.Connection, legacy_schema: str, state_dir: Path | None = None) -> None:
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
            version = 7
        if version == 7:
            _upgrade_v7_to_v8(db, state_dir or Path("."))
            db.execute("PRAGMA user_version = 8")
            version = 8
        if version == 8:
            _upgrade_v8_to_v9(db)
            db.execute("PRAGMA user_version = 9")
            version = 9
        if version == 9:
            _upgrade_v9_to_v10(db)
            db.execute("PRAGMA user_version = 10")
            version = 10
        if version == 10:
            _upgrade_v10_to_v11(db)
            db.execute("PRAGMA user_version = 11")
            version = 11
        if version == 11:
            _upgrade_v11_to_v12(db)
            db.execute("PRAGMA user_version = 12")
            version = 12
        if version == 12:
            _upgrade_v12_to_v13(db)
            db.execute("PRAGMA user_version = 13")
            version = 13
        if version == 13:
            _upgrade_v13_to_v14(db)
            db.execute("PRAGMA user_version = 14")
            version = 14
        if version == 14:
            _upgrade_v14_to_v15(db)
            db.execute("PRAGMA user_version = 15")
            version = 15
        if version == 15:
            _upgrade_v15_to_v16(db)
            db.execute("PRAGMA user_version = 16")
            version = 16
        if version == 16:
            _upgrade_v16_to_v17(db)
            db.execute("PRAGMA user_version = 17")
            version = 17
        if version == 17:
            _upgrade_v17_to_v18(db)
            db.execute("PRAGMA user_version = 18")
            version = 18
        if version == 18:
            upgrade_v18_to_v19(db)
            db.execute("PRAGMA user_version = 19")
        db.commit()
    except BaseException:
        db.rollback()
        raise
