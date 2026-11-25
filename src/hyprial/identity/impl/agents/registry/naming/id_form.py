# Decision record (UUID addressing §2.4, identity part of P4,
# docs/design/uuid-addressing-implementation-2026-11-03.md):
# - Runs on every registry open while id_form_minting() is on, and is
#   idempotent: a row already in id form is left alone, so the open after the
#   flip migrates and every later open is a no-op.
# - agents.uri: a stored name-form URI becomes canonical(owner, node, agent_id)
#   under the URI's own owner and node (hosted records keep their pinned
#   owner).  Everything this database keys by the URI follows in the same
#   transaction: binding:<uri> fences (resource, receipts and the payload's
#   actor), the agent-record fence payload when it still matched the row, and
#   the live incarnation's retired_session_refs rows.
# - Earlier incarnations of the same name keep their retired_session_refs
#   rows under the old name-form URI with their own entity token; the
#   retired-session fence matches refs by incarnation, not by name (see the
#   naming package).  No used name is recorded (owner decision 2026-11-19).
# - agent_secret_grants / retired_session_refs gain a nullable agent_id
#   backfilled from each row's entity_token.  The token stays the authority;
#   while id-form minting is on, each write also stamps the derived id on the
#   rows of its incarnation (stamp_agent_id), so a restart finds nothing to
#   repair and rewrites no persistent row (the 2026-11-03 flip follow-up's
#   hosted-row restart invariant).  Opens still repair rows written before it.
# - Homes stay keyed by the immutable actor (§2.1).  No agent-home:<agent_id>
#   row is written (integrator ruling D2): such a row had no reader and was
#   counted as a second home by agent-home: prefix scans.  Whether a home is
#   ever re-keyed by id (U3) is an open HQ question.
"""UUID addressing P4 for the agent registry database (identity part)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

from hyprial.kernel import canonical_agent_uri, is_agent_id_segment, parse_agent_uri

from .._base import AgentError, agent_id_for_entity_token


def migrate_registry_to_id_form(registry: Any) -> None:
    """Apply the identity half of P4 to ``registry``'s open database."""

    db: sqlite3.Connection = registry._db
    with registry._lock:
        with db:
            db.execute("BEGIN IMMEDIATE")
            _rewrite_agent_uris(registry, db)
            for table in ("agent_secret_grants", "retired_session_refs"):
                _backfill_agent_id_column(db, table)
        registry._refresh_retired_session_refs_locked()


def _rewrite_agent_uris(registry: Any, db: sqlite3.Connection) -> None:
    for row in db.execute("SELECT * FROM agents ORDER BY actor").fetchall():
        old_uri = str(row["uri"])
        parsed = parse_agent_uri(old_uri)
        if parsed is None or is_agent_id_segment(parsed[2]):
            continue
        try:
            agent_id = agent_id_for_entity_token(str(row["entity_token"]))
        except AgentError:
            continue
        actor = str(row["actor"])
        old = registry._row_agent(row)
        new = replace(old, uri=canonical_agent_uri(parsed[0], parsed[1], agent_id))
        db.execute("UPDATE agents SET uri = ? WHERE actor = ?", (new.uri, actor))
        move_binding_fence(db, old_uri, new.uri)
        record = db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
            (f"agent-record:{actor}",),
        ).fetchone()
        if record is not None and json.loads(str(record["payload"])) == old.to_json():
            db.execute(
                "UPDATE lifecycle_resources SET payload = ? WHERE resource_key = ?",
                (json.dumps(new.to_json(), sort_keys=True), f"agent-record:{actor}"),
            )
        db.execute(
            "UPDATE OR IGNORE retired_session_refs SET actor = ? "
            "WHERE actor = ? AND entity_token = ?",
            (new.uri, old_uri, old.entity_token),
        )


def move_binding_fence(db: sqlite3.Connection, old_uri: str, new_uri: str) -> None:
    """Re-key one ``binding:<uri>`` fence, its receipts and payload actor."""

    old_key, new_key = f"binding:{old_uri}", f"binding:{new_uri}"
    row = db.execute(
        "SELECT payload FROM lifecycle_resources WHERE resource_key = ?", (old_key,)
    ).fetchone()
    if row is None:
        return
    payload = json.loads(str(row["payload"]))
    if isinstance(payload, dict) and payload.get("actor") == old_uri:
        payload["actor"] = new_uri
    db.execute(
        "UPDATE OR IGNORE lifecycle_resources SET resource_key = ?, payload = ? "
        "WHERE resource_key = ?",
        (new_key, json.dumps(payload, sort_keys=True), old_key),
    )
    db.execute(
        "UPDATE lifecycle_receipts SET resource_key = ? WHERE resource_key = ?",
        (new_key, old_key),
    )


def _backfill_agent_id_column(db: sqlite3.Connection, table: str) -> None:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if "agent_id" not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN agent_id TEXT DEFAULT NULL")
    repairs: list[tuple[str, int]] = []
    for row in db.execute(
        f"SELECT rowid, entity_token FROM {table} WHERE agent_id IS NULL"
    ).fetchall():
        try:
            repairs.append((agent_id_for_entity_token(str(row[1])), int(row[0])))
        except AgentError:
            continue
    db.executemany(f"UPDATE {table} SET agent_id = ? WHERE rowid = ?", repairs)


def stamp_agent_id(db: sqlite3.Connection, table: str, entity_token: str) -> None:
    """Stamp the derived ``agent_id`` on one incarnation's freshly written rows.

    A no-op while id-form minting is off (the column is only added by the
    P4 open), for a token that derives no id, and for a table that has no
    ``agent_id`` column yet; any other database error (for example a locked
    database) propagates to the write it belongs to.
    """

    from hyprial.identity.impl.agents.addressing import id_form_minting

    if not id_form_minting():
        return
    try:
        agent_id = agent_id_for_entity_token(entity_token)
    except AgentError:
        return
    try:
        db.execute(
            f"UPDATE {table} SET agent_id = ? WHERE entity_token = ? AND agent_id IS NULL",
            (agent_id, entity_token),
        )
    except sqlite3.OperationalError as exc:
        if "no such column: agent_id" not in str(exc):
            raise


__all__ = ["migrate_registry_to_id_form", "move_binding_fence", "stamp_agent_id"]
