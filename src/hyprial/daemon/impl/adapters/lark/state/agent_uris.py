# Decision record (UUID addressing P4, external stores; spec §2.4 of
# docs/design/uuid-addressing-implementation-2026-11-03.md):
# - reply_routes rows are routing bindings: a native reply is sent to the
#   stored actor_key.  After the id-form flip a name-form agent URI there is a
#   retired address, so the one-time migration rewrites actor_id/actor_key to
#   the official URI the caller's mapping answers (the daemon's registry).
# - Message custody stays verbatim (ruling X): pending_inbound_submissions,
#   dead_letters, request_correlations and the dedup sets are not touched.
# - One BEGIN IMMEDIATE transaction per database, so concurrent adapter
#   processes queue on the write lock; the meta key below is the durable
#   marker that makes a second run a no-op.  A missing database is left
#   uncreated: nothing was routed, so nothing needs migrating.
"""One-time id-form migration of agent URIs in the Lark reply routes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.adapters.lark.state.records import _BUSY_TIMEOUT_MS

MIGRATION_MARKER_KEY = "agentUriIdFormMigration"


def migrate_reply_route_agent_uris(
    path: Path, mapping: Callable[[str], str | None]
) -> int | None:
    """Rewrite reply-route agent URIs once; None when already done or absent.

    ``mapping`` answers the official URI for a stored value, or None to keep
    it.  Returns the number of rewritten rows on the run that migrates.
    """

    if not Path(path).is_file():
        return None
    db = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            rewritten = _migrate(db, mapping)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return rewritten
    finally:
        db.close()


def _migrate(db: sqlite3.Connection, mapping: Callable[[str], str | None]) -> int | None:
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if db.execute(
        "SELECT 1 FROM meta WHERE key = ?", (MIGRATION_MARKER_KEY,)
    ).fetchone() is not None:
        return None
    rewritten = 0
    has_routes = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reply_routes'"
    ).fetchone() is not None
    rows = (
        db.execute(
            "SELECT adapter, message_id, actor_id, actor_key FROM reply_routes"
        ).fetchall()
        if has_routes
        else []
    )
    for adapter, message_id, actor_id, actor_key in rows:
        new_id = mapping(str(actor_id)) or str(actor_id)
        new_key = mapping(str(actor_key)) or str(actor_key)
        if (new_id, new_key) == (actor_id, actor_key):
            continue
        db.execute(
            "UPDATE reply_routes SET actor_id = ?, actor_key = ?"
            " WHERE adapter = ? AND message_id = ?",
            (new_id, new_key, adapter, message_id),
        )
        rewritten += 1
    db.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        (MIGRATION_MARKER_KEY, str(rewritten)),
    )
    return rewritten


__all__ = ["MIGRATION_MARKER_KEY", "migrate_reply_route_agent_uris"]
