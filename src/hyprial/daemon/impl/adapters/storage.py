"""SQLite adapter rows, kept separate from harness lifecycle tables."""
from __future__ import annotations

import json
from .rollback import BACKUP_SCHEMA, backup_document

ADAPTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapters (
    provider TEXT NOT NULL,
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('running', 'failed')),
    legacy_launch_json TEXT NOT NULL
);
""" + BACKUP_SCHEMA


def read_adapters(db):
    return [
        {"provider": row[0], "name": row[1],
         **({"status": row[2]} if row[2] != "running" else {}),
         **({"legacyLaunch": json.loads(row[3])} if row[3] != "{}" else {})}
        for row in db.execute("SELECT provider,name,status,legacy_launch_json FROM adapters ORDER BY rowid")
    ]


def insert_adapters(db, document):
    db.executemany(
        "INSERT INTO adapters(provider,name,status,legacy_launch_json) VALUES(?,?,?,?)",
        [(row["provider"], row["name"], row.get("status", "running"),
          json.dumps(row.get("legacyLaunch", {}), sort_keys=True, separators=(",", ":")))
         for row in document.get("adapters", [])],
    )


def migrate_adapters(shadow, schema):
    """Hold the SQLite writer lock through validation and atomic replacement."""
    from hyprial.daemon.impl.desired_state.documents import DesiredState

    if not shadow.has_document():
        return
    with shadow._state_db.transaction(schema=schema) as db:
        document = shadow.read_document()
        state = DesiredState.from_json(document)
        if document["schemaVersion"] == state.schema_version:
            return
        backup_document(db, document)
        insert_adapters(db, state.to_json())
        db.executemany("DELETE FROM harnesses WHERE harness=? AND name=?",
                       [(spec.provider, spec.name) for spec in state.adapters])
        db.execute("UPDATE root_state SET schema_version=? WHERE id=1", (state.schema_version,))
