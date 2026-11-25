# Decision record (UUID addressing P4, external stores; spec §2.4 of
# docs/design/uuid-addressing-implementation-2026-11-03.md):
# - A stored routine names agents in its spec (actor, produces, every
#   escalate_to) and in its owner column.  After the id-form flip a name-form
#   local agent URI there is a retired send address and no longer equals the
#   caller identity the daemon authorizes, so the one-time migration rewrites
#   each value the caller's mapping (the daemon's agent registry) answers.
#   Comparisons themselves resolve through the registry (integration ruling
#   D5); this only keeps the stored values official.
# - Every rewrite is ledgered in routine_address_migrations exactly like the
#   bare-name escalate_to migration, so the change is auditable through the
#   existing addressMigrations projection.
# - In-flight task rows, cycles and pending effects are records of work
#   already dispatched (outbound message intents, ruling X) and stay verbatim.
# - One transaction per database; the marker row makes a second run a no-op.
#   A missing database is left uncreated.
"""One-time id-form migration of agent URIs stored by self-drive routines."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from hyprial.biz.impl.routine.storage.store import RoutineStore

MARKER = "agent-uri-id-form"
_MARKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS routine_store_markers (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    at_ms INTEGER NOT NULL
)
"""


def migrate_routine_agent_uris(
    database: Path, mapping: Callable[[str], str | None], *, at_ms: int
) -> int | None:
    """Rewrite stored routine agent URIs once; None when already done or absent.

    Returns the number of rewritten fields on the run that migrates.
    """

    if not Path(database).is_file():
        return None
    store = RoutineStore(Path(database))
    try:
        with store._lock:
            db = store._db
            db.execute(_MARKER_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            try:
                rewritten = _migrate(db, mapping, at_ms)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            return rewritten
    finally:
        store.close()


def _migrate(db: Any, mapping: Callable[[str], str | None], at_ms: int) -> int | None:
    if db.execute(
        "SELECT 1 FROM routine_store_markers WHERE name = ?", (MARKER,)
    ).fetchone() is not None:
        return None
    rewritten = 0
    for row in db.execute(
        "SELECT name, yaml_text, owner FROM routines ORDER BY name"
    ).fetchall():
        rewritten += _migrate_row(db, row, mapping, at_ms)
    db.execute(
        "INSERT INTO routine_store_markers(name, value, at_ms) VALUES (?, ?, ?)",
        (MARKER, str(rewritten), at_ms),
    )
    return rewritten


def _migrate_row(db: Any, row: Any, mapping: Callable[[str], str | None], at_ms: int) -> int:
    name, yaml_text, owner = str(row["name"]), str(row["yaml_text"]), str(row["owner"])
    rewrites: list[tuple[str, str, str]] = []
    owner_after = mapping(owner)
    if owner_after is not None and owner_after != owner:
        rewrites.append(("owner", owner, owner_after))
    document = _document(yaml_text)
    spec_rewrites = [] if document is None else _rewrite_spec(document, mapping)
    rewrites.extend(spec_rewrites)
    if not rewrites:
        return 0
    if spec_rewrites:
        db.execute(
            "UPDATE routines SET yaml_text = ? WHERE name = ?",
            (yaml.safe_dump(document, sort_keys=False, allow_unicode=True), name),
        )
    if owner_after is not None and owner_after != owner:
        db.execute("UPDATE routines SET owner = ? WHERE name = ?", (owner_after, name))
    db.executemany(
        """INSERT OR IGNORE INTO routine_address_migrations
           (routine, field, before, after, migrated_at_ms) VALUES (?, ?, ?, ?, ?)""",
        [(name, field, before, after, at_ms) for field, before, after in rewrites],
    )
    return len(rewrites)


def _document(yaml_text: str) -> dict[str, Any] | None:
    try:
        document = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return None
    return document if isinstance(document, dict) else None


def _rewrite_spec(
    document: dict[str, Any], mapping: Callable[[str], str | None]
) -> list[tuple[str, str, str]]:
    slots: list[tuple[str, dict[str, Any], str]] = [
        (key, document, key) for key in ("actor", "produces")
    ]
    policy = document.get("policy")
    routes = policy.get("routes") if isinstance(policy, dict) else None
    if isinstance(routes, list):
        slots.extend(
            (f"policy.routes[{index}].escalate_to", item, "escalate_to")
            for index, item in enumerate(routes)
            if isinstance(item, dict)
        )
    timeout = document.get("on_task_timeout")
    if isinstance(timeout, dict):
        slots.append(("on_task_timeout.escalate_to", timeout, "escalate_to"))
    rewrites: list[tuple[str, str, str]] = []
    for field, holder, key in slots:
        value = holder.get(key)
        if not isinstance(value, str):
            continue
        after = mapping(value.strip())
        if after is None or after == value:
            continue
        holder[key] = after
        rewrites.append((field, value, after))
    return rewrites


__all__ = ["MARKER", "migrate_routine_agent_uris"]
