"""Remove later schema objects when constructing older DB fixtures."""

import sqlite3


def strip_held_v20(db: sqlite3.Connection) -> None:
    """Reconstruct dev's v19 workflow table and its original column triggers."""
    from hyprial.daemon.impl.pac.storage.migrations import _track

    for operation in ("insert", "delete", "update"):
        db.execute(f"DROP TRIGGER workflow_tick_workflow_graphs_{operation}")
    db.execute("ALTER TABLE workflow_graphs DROP COLUMN held_at_ms")
    _track(db, "workflow_graphs", ("graph_id",))
    db.execute("PRAGMA user_version = 19")


def strip_tick_v19(db: sqlite3.Connection) -> None:
    names = db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'workflow_tick_*'"
    ).fetchall()
    for row in names:
        name = row[0].replace('"', '""')
        db.execute(f'DROP TRIGGER "{name}"')
    for name in (
        'graphs_workflow_tick_open', 'workflow_tick_unfinished',
        'notifications_tick_alert', 'notifications_tick_pending',
    ):
        db.execute(f'DROP INDEX IF EXISTS "{name}"')
    db.execute('DROP TABLE IF EXISTS workflow_tick_projection')
    db.execute('PRAGMA user_version = 18')
