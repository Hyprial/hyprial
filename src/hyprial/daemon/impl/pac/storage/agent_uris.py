# Decision record (UUID addressing P4, external stores; spec §2.4 of
# docs/design/uuid-addressing-implementation-2026-11-03.md):
# - Rewrites the two AUTHORIZATION fields the schema-8 era migration already
#   established as rewritable, nodes.owner and graphs.created_by, from a
#   name-form local agent URI to the official URI the caller's mapping
#   answers.  After the flip an agent acts under its id URI, and the reactor
#   and edit paths compare these fields by exact string.
# - Historical facts stay verbatim, as in schema 8: flag_set_by, flag_events,
#   notifications (outbound message rows, ruling X), journal rows (append-only;
#   nothing is appended either, so consumer cursors do not move), remote
#   requests (signed grants) and outcome receipts.
# - HQ Q3: workflow worker addresses are excluded.  A node owned by a worker
#   (its URI is a worker receipt or cleanup intent actor_uri, or its name is
#   an actor node's actor_name) keeps its stored value, so the immutable
#   receipts and the roster check stay consistent until the PAC graph schema
#   upgrade decides wf-* addressing.
# - Opening the store runs the PAC schema upgrade (the same connect() the
#   PAC authority's bootstrap runs), so an older database is upgraded and
#   migrated in the same start.  The daemon calls this only after the legacy
#   workflow cutover succeeded and before the authority opens the store, so
#   the upgrade order is unchanged and migrated ordinary owners use official
#   addresses after the flip. The rewrite and durable marker commit in one
#   transaction.
# - workflow_graphs.roster_json/roster_digest is a digest-bound snapshot and
#   stays verbatim. A borrowed roster entry retains its historical spelling;
#   its ordinary node owner is the authorization field migrated above.
# - D13 managed-owner authorization keeps the verified caller official and
#   resolves the unchanged logical node owner only through a consistent
#   roster/receipt, its pinned entity token, and the current registry entity.
#   The runtime helper below implements that comparison without rewriting
#   node owners, roster bytes, receipts, or the deferred Q3 PAC schema.
"""Persisted URI migration and D13 runtime managed-owner resolution.

Runtime resolution leaves logical owners and immutable roster/receipts intact.
The daemon supplies a durable Agent read port; a managed receipt must prove
the current entity token before its official address can authorize a write.
Standalone engines without that port retain exact stored-owner comparison.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.pac.storage.store import NodeRow, PacGraphStore, connect
from hyprial.identity import Agent
from hyprial.kernel import parse_agent_uri

MARKER_TABLE = "agent_uri_migration"
AgentReader = Callable[[str], Agent | None]


def resolved_node_owner(
    store: PacGraphStore, node: NodeRow | None, read_agent: AgentReader | None = None,
) -> str | None:
    """Resolve only a receipt-proven managed owner to its live official identity."""

    if node is None:
        return None
    if read_agent is None:
        return node.owner
    binding = store._db.execute(
        "SELECT w.actor_node,r.actor_name,r.actor_uri,r.agent_entity_token "
        "FROM workflow_nodes w LEFT JOIN workflow_worker_receipts r "
        "ON r.graph_id=w.graph_id AND r.actor_node=w.actor_node "
        "WHERE w.graph_id=? AND w.node_id=?", (node.graph_id, node.node_id),
    ).fetchone()
    if binding is None or binding["actor_node"] is None:
        return node.owner
    token = binding["agent_entity_token"]
    if not token or binding["actor_uri"] != node.owner:
        return None
    parsed = parse_agent_uri(node.owner)
    if parsed is None:
        return None
    try:
        if store.workflow_roster(node.graph_id) is None:
            return None
        agent = read_agent(node.owner)
    except Exception:
        return None
    if (
        agent is not None and agent.entity_token == token
        and agent.actor == binding["actor_name"]
        and (agent.owner, agent.machine) == parsed[:2]
        and agent.hosted_by is None
    ):
        return agent.uri
    return None


def node_owner_matches(
    store: PacGraphStore, node: NodeRow | None, actor: str,
    read_agent: AgentReader | None = None,
) -> bool:
    return resolved_node_owner(store, node, read_agent) == actor


def migrate_pac_agent_uris(
    path: Path, mapping: Callable[[str], str | None], *, at_ms: int
) -> int | None:
    """Rewrite PAC authorization URIs once; None when already done or absent."""

    if not Path(path).is_file():
        return None
    db = connect(Path(path))
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            rewritten = _migrate(db, mapping, at_ms)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return rewritten
    finally:
        db.close()


def _worker_addresses(db: sqlite3.Connection) -> tuple[set[str], set[str]]:
    uris = {
        str(row[0])
        for table in ("workflow_worker_receipts", "workflow_worker_cleanup_intents")
        for row in db.execute(f"SELECT actor_uri FROM {table}")
    }
    names = {
        str(row[0])
        for row in db.execute(
            "SELECT actor_name FROM nodes WHERE actor_name IS NOT NULL"
        )
    }
    return uris, names


def _migrate(
    db: sqlite3.Connection, mapping: Callable[[str], str | None], at_ms: int
) -> int | None:
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), migrated_at_ms INTEGER NOT NULL, "
        "rewritten INTEGER NOT NULL, report_json TEXT NOT NULL)"
    )
    if db.execute(f"SELECT 1 FROM {MARKER_TABLE} WHERE id = 1").fetchone() is not None:
        return None
    worker_uris, worker_names = _worker_addresses(db)

    def official(value: str) -> str | None:
        parsed = parse_agent_uri(value)
        if value in worker_uris or (parsed is not None and parsed[2] in worker_names):
            return None
        return mapping(value)

    report: list[dict[str, str]] = []
    for graph_id, created_by in db.execute(
        "SELECT graph_id, created_by FROM graphs ORDER BY graph_id"
    ).fetchall():
        after = official(str(created_by))
        if after is not None and after != created_by:
            db.execute(
                "UPDATE graphs SET created_by = ? WHERE graph_id = ?", (after, graph_id)
            )
            report.append({"field": "graphs.created_by", "graphId": graph_id,
                           "before": created_by, "after": after})
    for graph_id, node_id, owner in db.execute(
        "SELECT graph_id, node_id, owner FROM nodes ORDER BY graph_id, node_id"
    ).fetchall():
        after = official(str(owner))
        if after is not None and after != owner:
            db.execute(
                "UPDATE nodes SET owner = ? WHERE graph_id = ? AND node_id = ?",
                (after, graph_id, node_id),
            )
            report.append({"field": "nodes.owner", "graphId": graph_id,
                           "nodeId": node_id, "before": owner, "after": after})
    db.execute(
        f"INSERT INTO {MARKER_TABLE}(id, migrated_at_ms, rewritten, report_json) "
        "VALUES (1, ?, ?, ?)",
        (at_ms, len(report), json.dumps(report, ensure_ascii=False)),
    )
    return len(report)


__all__ = ["MARKER_TABLE", "migrate_pac_agent_uris"]
