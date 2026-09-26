"""Explicit, atomic PAC schema migrations (slice-1 databases had version 0).

Do not use executescript inside the transaction: sqlite3 would commit before
running it. In particular future node-kind CHECK changes require a migration,
not a changed CREATE TABLE IF NOT EXISTS declaration.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from time import time_ns
from typing import Any
from uuid import uuid4

from .errors import PAC_MIGRATION_SOURCE_UNREADABLE, PacError
from .journal import JOURNAL_SCHEMA, append_event
from .principal import principal_kind

SCHEMA_VERSION = 13


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


def _upgrade_v7_to_v8(db: sqlite3.Connection, state_dir: Path) -> None:
    """The principal-URI era (design-pac-owner-full-uri; PR #536 review B1/B2).

    **B1 -- cursor neutrality**: this step appends NOTHING to the journal.
    ``cursorFloor`` and every consumer's cursor stay where they were; the
    public event contract's externally visible quantities do not move
    because of a schema bump.  The era marker is this ``user_version`` plus
    the snapshot's computed ``identityFormat``; the rewrite outcomes live in
    a dedicated metadata table (``schema_era``), not in the event stream.

    **B2 -- authorization continuity**: the v7 world wrote short-name
    owners while the v8 world authorizes exact principal URIs (G1=A), so a
    graph that kept its short names would be locked out of flag/close/stop.
    This migration rewrites the two AUTHORIZATION fields -- ``nodes.owner``
    and ``graphs.created_by`` -- to full principal URIs, but ONLY where the
    local agents registry / user profiles resolve the short name uniquely.
    Ambiguous or unresolvable values are kept VERBATIM (never guessed); the
    kept values are listed per graph in the ``schema_era`` report, and the
    authorization refusals for those graphs point at it.  Historical fact
    fields (``flag_set_by``, ``activated_by``/``closed_by``, notifications,
    journal rows) stay untouched (design §2.3).

    A source that exists but cannot be read (corrupt sqlite, malformed
    JSON) aborts the WHOLE migration: zero writes, loud failure -- "we
    could not look" is never folded into "unresolvable".  An absent source
    is a legitimate empty candidate set (a fresh home has no agents yet).
    """

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_era (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            user_version INTEGER NOT NULL,
            migrated_at_ms INTEGER NOT NULL,
            graphs_total INTEGER NOT NULL,
            owners_rewritten INTEGER NOT NULL,
            owners_kept INTEGER NOT NULL,
            report_json TEXT NOT NULL
        )
        """
    )
    registry_uris, profile_owners = _era_resolution_sources(state_dir)

    def resolve(short_name: str) -> tuple[str | None, str | None]:
        """("user:<owner>" | "agent:…" | None, reason for keeping)."""

        candidates = set()
        if short_name in profile_owners:
            candidates.add(f"user:{short_name}")
        if short_name in registry_uris:
            candidates.add(registry_uris[short_name])
        if len(candidates) == 1:
            return candidates.pop(), None
        if len(candidates) > 1:
            return None, "ambiguous"
        return None, "unresolvable"

    report: dict[str, Any] = {"graphs": []}
    graphs_total = owners_rewritten = owners_kept = 0
    for graph in db.execute(
        "SELECT graph_id, created_by FROM graphs ORDER BY graph_id"
    ).fetchall():
        graphs_total += 1
        kept: list[dict[str, str]] = []
        graph_rewritten = 0
        if principal_kind(graph["created_by"]) is None:
            rewritten_uri, keep_reason = resolve(graph["created_by"])
            if rewritten_uri is not None:
                db.execute(
                    "UPDATE graphs SET created_by=? WHERE graph_id=?",
                    (rewritten_uri, graph["graph_id"]),
                )
                owners_rewritten += 1
                graph_rewritten += 1
            else:
                owners_kept += 1
                kept.append(
                    {"field": "graphs.created_by", "value": graph["created_by"],
                     "reason": keep_reason or "unresolvable"}
                )
        for node in db.execute(
            "SELECT node_id, owner FROM nodes WHERE graph_id=? ORDER BY node_id",
            (graph["graph_id"],),
        ).fetchall():
            if principal_kind(node["owner"]) is not None:
                continue  # already a full principal URI
            rewritten_uri, keep_reason = resolve(node["owner"])
            if rewritten_uri is not None:
                db.execute(
                    "UPDATE nodes SET owner=? WHERE graph_id=? AND node_id=?",
                    (rewritten_uri, graph["graph_id"], node["node_id"]),
                )
                owners_rewritten += 1
                graph_rewritten += 1
            else:
                owners_kept += 1
                kept.append(
                    {"field": "nodes.owner", "nodeId": node["node_id"],
                     "value": node["owner"], "reason": keep_reason or "unresolvable"}
                )
        report["graphs"].append(
            {"graphId": graph["graph_id"],
             "rewritten": graph_rewritten,
             "kept": kept}
        )
    # rewrite counts per graph: recompute from the kept side is not enough;
    # keep the per-graph rewritten count alongside
    db.execute(
        "INSERT OR REPLACE INTO schema_era (id, user_version, migrated_at_ms, "
        "graphs_total, owners_rewritten, owners_kept, report_json) "
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        (
            # The era report belongs to THIS step; a later bump must not
            # relabel it (a v7 database migrated straight to v9 was still
            # rewritten by the schema-8 step).
            8,
            time_ns() // 1_000_000,
            graphs_total,
            owners_rewritten,
            owners_kept,
            json.dumps(report, ensure_ascii=False),
        ),
    )


def _upgrade_v8_to_v9(db: sqlite3.Connection) -> None:
    """Add the graph-linked projection/outbox for frozen ``agent.task``.

    These rows live with their PAC graph, never in the legacy workflow
    database.  The opaque request/activity/result bodies stay outside graph
    references while foreign keys make it impossible to retain a task without
    its authoritative graph.
    """

    db.execute(
        """
        CREATE TABLE pac_agent_task_runs (
            graph_id         TEXT PRIMARY KEY REFERENCES graphs(graph_id),
            service_actor    TEXT NOT NULL,
            namespace        TEXT NOT NULL,
            external_ref     TEXT NOT NULL,
            request_digest   TEXT NOT NULL,
            caller           TEXT NOT NULL,
            metadata_json    TEXT NOT NULL,
            payload_json     TEXT NOT NULL,
            completion_json  TEXT NOT NULL,
            state            TEXT NOT NULL
                             CHECK(state IN ('reserved','running','waiting','completed','failed','cancelled')),
            last_event_id    TEXT,
            cancel_reason    TEXT,
            created_at_ms    INTEGER NOT NULL,
            finished_at_ms   INTEGER,
            UNIQUE(service_actor, namespace, external_ref)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE pac_agent_task_targets (
            graph_id        TEXT NOT NULL REFERENCES pac_agent_task_runs(graph_id),
            ordinal         INTEGER NOT NULL,
            target_ref      TEXT NOT NULL,
            node_id         TEXT NOT NULL,
            target          TEXT NOT NULL,
            role            TEXT NOT NULL CHECK(role IN ('owner','participant')),
            delegates_json  TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            state           TEXT NOT NULL
                            CHECK(state IN ('reserved','dispatching','running','waiting','completed','failed','cancelled')),
            result_ref      TEXT,
            PRIMARY KEY(graph_id, target_ref),
            UNIQUE(graph_id, ordinal),
            UNIQUE(graph_id, node_id),
            UNIQUE(graph_id, target),
            UNIQUE(graph_id, conversation_id),
            FOREIGN KEY(graph_id, node_id) REFERENCES nodes(graph_id, node_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE pac_agent_task_dispatches (
            graph_id        TEXT NOT NULL,
            target_ref      TEXT NOT NULL,
            effect_id       TEXT NOT NULL UNIQUE,
            text            TEXT NOT NULL,
            message_id      TEXT,
            delivered_at_ms INTEGER,
            PRIMARY KEY(graph_id, target_ref),
            FOREIGN KEY(graph_id, target_ref)
                REFERENCES pac_agent_task_targets(graph_id, target_ref)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE pac_agent_task_events (
            seq             INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL UNIQUE,
            event_digest    TEXT NOT NULL,
            graph_id        TEXT NOT NULL,
            target_ref      TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            kind            TEXT NOT NULL,
            submitter       TEXT NOT NULL,
            at              TEXT NOT NULL,
            payload_json    TEXT NOT NULL,
            message_id      TEXT NOT NULL,
            FOREIGN KEY(graph_id, target_ref)
                REFERENCES pac_agent_task_targets(graph_id, target_ref)
        )
        """
    )
    db.execute(
        "CREATE INDEX pac_agent_task_events_graph_order "
        "ON pac_agent_task_events(graph_id, seq)"
    )
    db.execute(
        """
        CREATE TABLE pac_agent_task_results (
            graph_id          TEXT NOT NULL,
            target_ref        TEXT NOT NULL,
            result_ref        TEXT NOT NULL,
            result_digest     TEXT NOT NULL,
            message_id        TEXT NOT NULL,
            payload_json      TEXT NOT NULL,
            artifact_refs_json TEXT NOT NULL,
            submitted_at      TEXT NOT NULL,
            activity_event_id TEXT NOT NULL UNIQUE,
            flag_event_id     TEXT NOT NULL UNIQUE,
            PRIMARY KEY(graph_id, target_ref),
            UNIQUE(graph_id, target_ref, result_ref),
            FOREIGN KEY(graph_id, target_ref)
                REFERENCES pac_agent_task_targets(graph_id, target_ref),
            FOREIGN KEY(flag_event_id) REFERENCES flag_events(event_id)
        )
        """
    )


def _era_resolution_sources(state_dir: Path) -> tuple[dict[str, str], set[str]]:
    """(registry actor -> canonical uri, profile owner names) for rewrites.

    Read-only against the REAL serializations.  A source that exists but
    cannot be parsed raises PAC_MIGRATION_SOURCE_UNREADABLE -- the migration
    then aborts as a whole (zero writes) instead of treating every owner as
    unresolvable.
    """

    registry_uris: dict[str, str] = {}
    database = Path(state_dir) / "agents.sqlite3"
    if database.is_file():
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT actor, uri FROM agents"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise PacError(
                PAC_MIGRATION_SOURCE_UNREADABLE,
                f"cannot read the agents registry at {database}: {error}; "
                "the schema-8 migration refuses to rewrite owners half-blind",
            ) from error
        registry_uris = {str(row[0]): str(row[1]) for row in rows}

    profile_owners: set[str] = set()
    store_path = Path(state_dir) / "users.json"
    if store_path.is_file():
        try:
            document = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PacError(
                PAC_MIGRATION_SOURCE_UNREADABLE,
                f"cannot read the user profiles at {store_path}: {error}; "
                "the schema-8 migration refuses to rewrite owners half-blind",
            ) from error
        users = document.get("users") if isinstance(document, dict) else None
        if not isinstance(users, list):
            raise PacError(
                PAC_MIGRATION_SOURCE_UNREADABLE,
                f"user profiles at {store_path} have an unsupported shape; "
                "the schema-8 migration refuses to rewrite owners half-blind",
            )
        profile_owners = {
            str(profile["owner"])
            for profile in users
            if isinstance(profile, dict) and isinstance(profile.get("owner"), str)
        }
    return registry_uris, profile_owners


def unrewritten_owners_note(db: sqlite3.Connection, graph_id: str) -> str | None:
    """The B2 pointer for authorization refusals on a half-migrated graph."""

    try:
        row = db.execute(
            "SELECT report_json FROM schema_era WHERE id=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # pre-v8 database: the pointer surface does not exist yet
    if row is None:
        return None
    try:
        report = json.loads(row[0])
    except ValueError:
        return None
    graph = next(
        (item for item in report.get("graphs", [])
         if isinstance(item, dict) and item.get("graphId") == graph_id),
        None,
    )
    if graph is None or not graph.get("kept"):
        return None
    return (
        "this graph still carries pre-URI short-name owners the schema-8 "
        "migration could not rewrite (unresolvable or ambiguous); "
        "`hyprial workflow migration status` lists them -- re-create the "
        "affected nodes with full principal URIs"
    )


def _upgrade_v9_to_v10(db: sqlite3.Connection) -> None:
    """Add graph workflow projections without moving legacy workflow runs."""
    db.execute(JOURNAL_SCHEMA.replace("CREATE TABLE journal", "CREATE TABLE journal_next", 1))
    db.execute("INSERT INTO journal_next SELECT * FROM journal")
    db.execute("DROP TABLE journal")
    db.execute("ALTER TABLE journal_next RENAME TO journal")
    db.execute("CREATE INDEX journal_graph_order ON journal(graph_id, seq)")
    db.execute("CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'PAC journal is append-only'); END")
    db.execute("CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT, 'PAC journal is append-only'); END")
    db.execute("""
        CREATE TABLE workflow_graphs (
            graph_id TEXT PRIMARY KEY REFERENCES graphs(graph_id),
            specification_ref TEXT NOT NULL,
            specification_digest TEXT NOT NULL,
            on_failure TEXT NOT NULL CHECK(on_failure IN ('terminate','continue','hold')),
            state TEXT NOT NULL CHECK(state IN ('running','held','completed','failed','cancelled')),
            reason_ref TEXT,
            routine_name TEXT,
            task_key TEXT
        )
    """)
    db.execute("""
        CREATE TABLE workflow_nodes (
            graph_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            actor_node TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','requested','done','failed','blocked','cancelled')),
            request_id TEXT,
            input_token TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            deadline_ms INTEGER NOT NULL,
            reason_ref TEXT,
            PRIMARY KEY(graph_id,node_id),
            FOREIGN KEY(graph_id,node_id) REFERENCES nodes(graph_id,node_id)
        )
    """)
    db.execute("CREATE INDEX workflow_nodes_request ON workflow_nodes(request_id)")


def _upgrade_v10_to_v11(db: sqlite3.Connection) -> None:
    """Bind queued message identities before handing them to the inbox."""
    from hyprial.dispatch.identity import dispatch_message_id

    db.execute("""
        CREATE TABLE workflow_deliveries (
            message_id TEXT PRIMARY KEY,
            graph_id TEXT NOT NULL REFERENCES graphs(graph_id),
            node_id TEXT NOT NULL,
            request_id TEXT NOT NULL
        )
    """)
    db.execute("CREATE INDEX notifications_graph_pending ON notifications(json_extract(plan_json,'$.graphId'),message_id)")
    for row in db.execute("SELECT event_id,edge,plan_json FROM notifications WHERE event_id LIKE 'workflow-request:%'"):
        plan = json.loads(row["plan_json"])
        message_id = dispatch_message_id(f"pac:pac-notify:{row['event_id']}:{row['edge']}")
        db.execute("INSERT INTO workflow_deliveries VALUES (?,?,?,?)",
                   (message_id, plan["graphId"], plan["nodeId"], row["event_id"]))


def _upgrade_v11_to_v12(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE remote_workflow_key (singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret BLOB NOT NULL)")
    db.execute("""CREATE TABLE remote_workflow_requests (
        request_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, node_id TEXT NOT NULL,
        owner TEXT NOT NULL, origin TEXT NOT NULL, message_id TEXT UNIQUE NOT NULL,
        deadline_ms INTEGER NOT NULL, grant_json TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE remote_workflow_outbox (
        request_id TEXT PRIMARY KEY REFERENCES remote_workflow_requests(request_id),
        action TEXT NOT NULL, reason_ref TEXT NOT NULL, result_json TEXT,
        attempted_at INTEGER NOT NULL DEFAULT 0
    )""")
    db.execute("CREATE INDEX remote_workflow_graph ON remote_workflow_requests(graph_id,node_id)")
    db.execute("""CREATE TABLE workflow_outcome_receipts (
        request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
        reason_ref TEXT, result_json TEXT NOT NULL
    )""")


def _upgrade_v12_to_v13(db: sqlite3.Connection) -> None:
    """Start relative workflow timeouts at their first request.

    Existing rows retain their absolute deadline.  Their original relative
    timeout was not stored, so migration deliberately leaves ``timeout_ms``
    NULL instead of reconstructing it from unrelated timestamps.
    """
    db.execute(
        """
        CREATE TABLE workflow_nodes_v13 (
            graph_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            actor_node TEXT,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','requested','done','failed','blocked','cancelled')),
            request_id TEXT,
            input_token TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            deadline_ms INTEGER,
            timeout_ms INTEGER,
            reason_ref TEXT,
            PRIMARY KEY(graph_id,node_id),
            FOREIGN KEY(graph_id,node_id) REFERENCES nodes(graph_id,node_id)
        )
        """
    )
    db.execute(
        """
        INSERT INTO workflow_nodes_v13
            (graph_id,node_id,actor_node,state,request_id,input_token,generation,
             deadline_ms,timeout_ms,reason_ref)
        SELECT graph_id,node_id,actor_node,state,request_id,input_token,generation,
               deadline_ms,NULL,reason_ref
        FROM workflow_nodes
        """
    )
    db.execute("DROP TABLE workflow_nodes")
    db.execute("ALTER TABLE workflow_nodes_v13 RENAME TO workflow_nodes")
    db.execute("CREATE INDEX workflow_nodes_request ON workflow_nodes(request_id)")


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
        db.commit()
    except BaseException:
        db.rollback()
        raise
