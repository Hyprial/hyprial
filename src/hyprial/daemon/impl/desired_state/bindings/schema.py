"""SQLite row projection; callers supply the existing StateDatabase transaction."""

import json

from hyprial.kernel import DesiredStateError
from .codec import ROOTS, parse, project
from .models import Binding, Cleanup, Configuration, Operation, canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_bindings (
 agent_id TEXT NOT NULL, member_id TEXT NOT NULL, binding_ref TEXT NOT NULL UNIQUE,
 binding_revision INTEGER NOT NULL CHECK(binding_revision > 0), configuration_json TEXT NOT NULL,
 operation_id TEXT NOT NULL, PRIMARY KEY(agent_id, member_id)
);
CREATE TABLE IF NOT EXISTS model_binding_operations (
 agent_id TEXT NOT NULL, member_id TEXT NOT NULL, operation_id TEXT NOT NULL,
 request_digest TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK(expected_revision >= 0),
 binding_ref TEXT NOT NULL, candidate_json TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('prepared','finalized','cancelled')), result_revision INTEGER,
 PRIMARY KEY(agent_id, member_id, operation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS model_binding_one_prepared
 ON model_binding_operations(agent_id, member_id) WHERE phase='prepared';
CREATE TABLE IF NOT EXISTS model_agent_lifecycle (
 agent_id TEXT PRIMARY KEY, entity_token TEXT NOT NULL, destroy_attempt TEXT NOT NULL
);
"""


def migrate_launch_reference(state_db):
    """Add the optional snapshot without repeating historical ghost resolution."""
    with state_db.read() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(harnesses)")}
    if not columns or "model_binding_json" in columns:
        return
    with state_db.transaction() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(harnesses)")}
        if "model_binding_json" not in columns:
            db.execute("ALTER TABLE harnesses ADD COLUMN model_binding_json TEXT")


def binding(row):
    if row is None:
        return None
    value = Binding(
        row[0],
        row[1],
        row[2],
        row[3],
        Configuration.from_json(json.loads(row[4])),
        row[5],
    )
    return Binding.from_json(value.to_json())


def operation(row):
    if row is None:
        return None
    value = Operation(*row[:6], Configuration.from_json(json.loads(row[6])), *row[7:])
    return Operation.from_json(value.to_json())


def read(db):
    bindings = tuple(
        binding(row)
        for row in db.execute(
            "SELECT agent_id,member_id,binding_ref,binding_revision,configuration_json,operation_id FROM model_bindings ORDER BY agent_id,member_id"
        )
    )
    operations = tuple(
        operation(tuple(row))
        for row in db.execute(
            "SELECT agent_id,member_id,operation_id,request_digest,expected_revision,binding_ref,candidate_json,phase,result_revision FROM model_binding_operations ORDER BY agent_id,member_id,operation_id"
        )
    )
    cleanup = tuple(
        Cleanup(*row)
        for row in db.execute(
            "SELECT agent_id,entity_token,destroy_attempt FROM model_agent_lifecycle ORDER BY agent_id"
        )
    )
    result = project(bindings, operations, cleanup)
    parse({"schemaVersion": 4, **result})
    return result


def insert_binding(db, item):
    db.execute(
        "INSERT INTO model_bindings VALUES(?,?,?,?,?,?) ON CONFLICT(agent_id,member_id) DO UPDATE SET binding_revision=excluded.binding_revision,configuration_json=excluded.configuration_json,operation_id=excluded.operation_id",
        (
            item.agent_id,
            item.member_id,
            item.binding_ref,
            item.binding_revision,
            canonical(item.configuration.to_json()),
            item.operation_id,
        ),
    )


def insert_operation(db, item):
    db.execute(
        "INSERT INTO model_binding_operations VALUES(?,?,?,?,?,?,?,?,?)",
        (
            item.agent_id,
            item.member_id,
            item.operation_id,
            item.request_digest,
            item.expected_revision,
            item.binding_ref,
            canonical(item.candidate.to_json()),
            item.phase,
            item.result_revision,
        ),
    )


def replace_projection(db, document):
    values = parse(document)
    for table in (
        "model_bindings",
        "model_binding_operations",
        "model_agent_lifecycle",
    ):
        db.execute(f"DELETE FROM {table}")
    for item in values[0]:
        insert_binding(db, item)
    for item in values[1]:
        insert_operation(db, item)
    db.executemany(
        "INSERT INTO model_agent_lifecycle VALUES(?,?,?)",
        [
            (item.agent_id, item.entity_token, item.destroy_attempt)
            for item in values[2]
        ],
    )


def guard_replace(db, document):
    previous = db.execute("SELECT schema_version FROM root_state WHERE id=1").fetchone()
    if previous and previous[0] >= 4:
        if (
            previous[0] != 4
            or document.get("schemaVersion") != 4
            or read(db) != {key: document.get(key) for key in ROOTS}
        ):
            raise DesiredStateError(
                "stale model binding document cannot replace committed authority"
            )
    parse(document)


def activate(db):
    row = db.execute("SELECT schema_version FROM root_state WHERE id=1").fetchone()
    if row and row[0] not in (1, 2, 3, 4):
        raise DesiredStateError("unsupported desired-state schema version")
    db.execute(
        "INSERT INTO root_state VALUES(1,4,0) ON CONFLICT(id) DO UPDATE SET schema_version=4"
    )
