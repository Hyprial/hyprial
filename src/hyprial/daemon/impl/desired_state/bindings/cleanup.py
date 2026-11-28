"""Incarnation-scoped destroy fencing; identity deletion is owned by PR4 callers."""

from .models import Cleanup, fail
from .schema import activate


def mark(db, agent_id, entity_token, attempt, expected_attempt):
    row = db.execute(
        "SELECT entity_token,destroy_attempt FROM model_agent_lifecycle WHERE agent_id=?",
        (agent_id,),
    ).fetchone()
    if row == (entity_token, attempt):
        return Cleanup(agent_id, entity_token, attempt)
    if row is None:
        if expected_attempt is not None:
            fail("revision-mismatch")
    elif row != (entity_token, expected_attempt):
        fail("revision-mismatch")
    activate(db)
    db.execute(
        "INSERT INTO model_agent_lifecycle VALUES(?,?,?) ON CONFLICT(agent_id) DO UPDATE SET destroy_attempt=excluded.destroy_attempt",
        (agent_id, entity_token, attempt),
    )
    return Cleanup(agent_id, entity_token, attempt)


def clear(db, agent_id, entity_token, attempt, *, purge=False):
    row = db.execute(
        "SELECT entity_token,destroy_attempt FROM model_agent_lifecycle WHERE agent_id=?",
        (agent_id,),
    ).fetchone()
    if row is None:
        if purge and any(
            db.execute(
                f"SELECT 1 FROM {table} WHERE agent_id=? LIMIT 1", (agent_id,)
            ).fetchone()
            for table in ("model_bindings", "model_binding_operations")
        ):
            fail("revision-mismatch")
        return
    if row != (entity_token, attempt):
        fail("revision-mismatch")
    if purge:
        for table in ("model_bindings", "model_binding_operations"):
            db.execute(f"DELETE FROM {table} WHERE agent_id=?", (agent_id,))
    db.execute("DELETE FROM model_agent_lifecycle WHERE agent_id=?", (agent_id,))
