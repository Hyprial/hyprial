"""Conditional binding changes execute entirely in the caller's SQLite transaction."""

from dataclasses import replace
from uuid import uuid4

from hyprial.error import codes
from .models import (
    Binding,
    Operation,
    MODEL_BINDING_OPERATION_LIMIT,
    fail,
    request_digest,
)
from .schema import activate, binding, operation, insert_binding, insert_operation


def require_open(db, agent_id):
    if db.execute(
        "SELECT 1 FROM model_agent_lifecycle WHERE agent_id=?", (agent_id,)
    ).fetchone():
        fail("destroy-cleanup-pending", codes.MODEL_CONFIGURATION_INCOMPLETE)


def current(db, agent_id, member_id):
    return binding(
        db.execute(
            "SELECT * FROM model_bindings WHERE agent_id=? AND member_id=?",
            (agent_id, member_id),
        ).fetchone()
    )


def receipt(db, agent_id, member_id, operation_id):
    return operation(
        db.execute(
            "SELECT * FROM model_binding_operations WHERE agent_id=? AND member_id=? AND operation_id=?",
            (agent_id, member_id, operation_id),
        ).fetchone()
    )


def prepare(
    db,
    agent_id,
    member_id,
    operation_id,
    expected_revision,
    candidate,
    operation_limit=MODEL_BINDING_OPERATION_LIMIT,
):
    require_open(db, agent_id)
    digest = request_digest(agent_id, member_id, expected_revision, candidate)
    previous = receipt(db, agent_id, member_id, operation_id)
    if previous is not None:
        if previous.request_digest != digest:
            fail("operation-digest-mismatch")
        return previous
    pending = db.execute(
        "SELECT 1 FROM model_binding_operations WHERE agent_id=? AND member_id=? AND phase='prepared'",
        (agent_id, member_id),
    ).fetchone()
    if pending:
        fail("operation-in-progress", codes.MODEL_CONFIGURATION_INCOMPLETE)
    old = current(db, agent_id, member_id)
    if (old.binding_revision if old else 0) != expected_revision:
        fail("revision-mismatch")
    if (
        type(operation_limit) is not int
        or not MODEL_BINDING_OPERATION_LIMIT <= operation_limit <= 2**31 - 1
    ):
        fail("operation-limit-setting", codes.MODEL_CONFIGURATION_INVALID, "validate")
    count = db.execute(
        "SELECT COUNT(*) FROM model_binding_operations WHERE agent_id=? AND member_id=?",
        (agent_id, member_id),
    ).fetchone()[0]
    if count >= operation_limit:
        fail("operation-capacity", codes.MODEL_OPERATION_CAPACITY_EXCEEDED)
    item = Operation(
        agent_id,
        member_id,
        operation_id,
        digest,
        expected_revision,
        old.binding_ref if old else f"mb.{uuid4().hex}",
        candidate,
    )
    activate(db)
    insert_operation(db, item)
    return item


def finish(
    db, agent_id, member_id, operation_id, digest, expected_revision, *, cancel=False
):
    require_open(db, agent_id)
    item = receipt(db, agent_id, member_id, operation_id)
    if item is None:
        fail("revision-mismatch")
    if item.request_digest != digest:
        fail("operation-digest-mismatch")
    if item.expected_revision != expected_revision:
        fail("revision-mismatch")
    if item.phase != "prepared":
        if item.phase != ("cancelled" if cancel else "finalized"):
            fail("operation-already-completed")
        return item
    old = current(db, agent_id, member_id)
    if (old.binding_revision if old else 0) != expected_revision:
        fail("revision-mismatch")
    result = replace(
        item,
        phase="cancelled" if cancel else "finalized",
        result_revision=expected_revision + (not cancel),
    )
    if not cancel:
        insert_binding(
            db,
            Binding(
                agent_id,
                member_id,
                item.binding_ref,
                result.result_revision,
                item.candidate,
                item.operation_id,
            ),
        )
    db.execute(
        "UPDATE model_binding_operations SET phase=?,result_revision=? WHERE agent_id=? AND member_id=? AND operation_id=? AND phase='prepared'",
        (result.phase, result.result_revision, agent_id, member_id, operation_id),
    )
    return result
