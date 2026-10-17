"""Internal local intent journal; no effect execution or restore authority.

Only the P2 writer may mutate it. A new begin persists intent before granting
one execution ticket; replay returns execute=False. A pending ticket after a
process crash is UNKNOWN until local authority/remote receipts are reconciled.
This layer never retries effects, releases post-intent custody, or certifies a
remote receipt. Production coordinator and restore wiring are separate slices.
"""

from dataclasses import dataclass, replace
import sqlite3
from typing import TYPE_CHECKING

from .model import AdmissionConflict, Reason, Role, identifier

if TYPE_CHECKING:
    from .store import AdmissionStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_p2_steps (
 operation_id TEXT NOT NULL,
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 actor TEXT NOT NULL,
 phase TEXT NOT NULL,
 step TEXT NOT NULL,
 fingerprint TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation>0),
 entity_token TEXT NOT NULL,
 receipt TEXT NOT NULL,
 needs_attention INTEGER NOT NULL CHECK(needs_attention IN (0,1)),
 PRIMARY KEY(operation_id,generation)
);
CREATE TABLE IF NOT EXISTS transfer_p2_attention (
 event_id TEXT PRIMARY KEY,
 operation_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 principal TEXT NOT NULL,
 action TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',
 recorded_at_ms INTEGER NOT NULL
);
"""

# Journal metadata, not a second custody authority. Triggers put the revision
# increment in the same transaction as intent/admission/result mutation.
SCHEMA += """
CREATE TABLE IF NOT EXISTS transfer_p2_revision (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 version INTEGER NOT NULL CHECK(version>=0)
);
INSERT OR IGNORE INTO transfer_p2_revision VALUES (1,0);
"""
for _table in ("transfer_p2_admissions", "transfer_p2_steps"):
    for _event in ("INSERT", "UPDATE", "DELETE"):
        SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS {_table}_revision_{_event.lower()}
AFTER {_event} ON {_table}
BEGIN UPDATE transfer_p2_revision SET version=version+1 WHERE singleton=1; END;
"""


@dataclass(frozen=True, slots=True)
class StepState:
    operation_id: str
    actor: str
    phase: str
    step: str
    fingerprint: str
    generation: int
    entity_token: str
    receipt: str
    needs_attention: bool = False


@dataclass(frozen=True, slots=True)
class StepStart:
    state: StepState
    execute: bool


@dataclass(frozen=True, slots=True)
class Custody:
    operations: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.operations)


@dataclass(frozen=True, slots=True)
class AttentionEvent:
    event_id: str
    operation_id: str
    generation: int
    principal: str
    action: str
    reason: str
    recorded_at_ms: int


_COMPLETED = {"quiesce": "quiesced", "land": "landed", "activate": "active"}

# Bounded free text on the audit row; long enough for an operator sentence and
# short enough that a journal write stays proportional to one operation.
MAX_ATTENTION_REASON = 500


def _read(conn: sqlite3.Connection, operation_id: str,
          generation: int | None = None) -> StepState | None:
    query = "SELECT * FROM transfer_p2_steps WHERE operation_id=?"
    args = (operation_id,)
    if generation is not None:
        query += " AND generation=?"
        args = (operation_id, generation)
    row = conn.execute(query + " ORDER BY generation DESC LIMIT 1", args).fetchone()
    if row is None:
        return None
    op, version, actor, phase, step, fingerprint, generation, token, receipt, attention = row
    if (version != 1 or step not in _COMPLETED
            or phase not in (f"{step}-pending", _COMPLETED[step])
            or type(generation) is not int or generation < 1 or attention not in (0, 1)):
        raise AdmissionConflict(Reason.CORRUPT_RECORD)
    try:
        for value in (op, actor, fingerprint):
            identifier(value)
        if phase.endswith("-pending"):
            if receipt:
                raise ValueError("pending record has a receipt")
        else:
            identifier(token)
            identifier(receipt)
    except ValueError as error:
        raise AdmissionConflict(Reason.CORRUPT_RECORD) from error
    return StepState(op, actor, phase, step, fingerprint, generation, token, receipt, bool(attention))


class PhaseJournal:
    """Use the admission's shared DB, binding checks and single-writer owner.

    Methods are internal, not IPC. Principal/receipt arguments must already be
    admitted/verified by the future coordinator; their strings are not proof.
    There is deliberately no terminal release method until fenced completion
    and abort are implemented. The prepare-only cancellation path refuses any
    existing step row, including a corrupt or attention-marked one.
    """

    def __init__(self, admission: "AdmissionStore") -> None:
        self._admission = admission

    def get(self, operation_id: str) -> StepState | None:
        with self._admission._database.read() as conn:
            self._admission._require(conn, operation_id)
            return _read(conn, operation_id)

    def begin(self, operation_id: str, *, step: str, fingerprint: str,
              actor: str) -> StepStart:
        identifier(fingerprint)
        identifier(actor)
        if step not in _COMPLETED:
            raise ValueError("unsupported local step")
        with self._admission._database.transaction() as conn:
            admission = self._admission._require(conn, operation_id)
            current = _read(conn, operation_id)
            if current is not None and current.step == step:
                if current.actor != actor or current.fingerprint != fingerprint:
                    raise AdmissionConflict(Reason.REPLAY_MISMATCH)
                # Returning a stored result/intention is not a new execution.
                return StepStart(current, False)
            admission.require_live(self._admission._clock())
            if (admission.binding.role is Role.SOURCE and step != "quiesce"
                    or admission.binding.role is Role.TARGET and step == "quiesce"):
                raise AdmissionConflict(Reason.PHASE_CONFLICT)
            if admission.binding.role is Role.TARGET and actor != admission.binding.target_actor:
                raise AdmissionConflict(Reason.BINDING_MISMATCH)
            if current is None:
                if step == "activate":
                    raise AdmissionConflict(Reason.PHASE_CONFLICT)
                generation = 1
                token = (admission.binding.source_entity_token
                         if admission.binding.role is Role.SOURCE else "")
            else:
                if (step != "activate" or current.phase != "landed"
                        or current.actor != actor or current.needs_attention):
                    raise AdmissionConflict(Reason.PHASE_CONFLICT)
                generation = current.generation + 1
                token = current.entity_token
            state = StepState(operation_id, actor, f"{step}-pending", step,
                              fingerprint, generation, token, "")
            conn.execute("INSERT INTO transfer_p2_steps VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (operation_id, 1, actor, state.phase, step, fingerprint, generation,
                          token, "", 0))
            return StepStart(state, True)

    def finish(self, operation_id: str, *, expected: StepState,
               receipt: str, entity_token: str) -> StepState:
        identifier(receipt)
        identifier(entity_token)
        if (not isinstance(expected, StepState) or expected.operation_id != operation_id
                or expected.step not in _COMPLETED or expected.phase != f"{expected.step}-pending"):
            raise AdmissionConflict(Reason.PHASE_CONFLICT)
        completed = replace(expected, phase=_COMPLETED[expected.step],
                            receipt=receipt, entity_token=entity_token)
        with self._admission._database.transaction() as conn:
            admission = self._admission._require(conn, operation_id)
            current = _read(conn, operation_id)
            if current == completed:
                return current
            if current != expected or current.needs_attention:
                raise AdmissionConflict(Reason.PHASE_CONFLICT)
            if (admission.binding.role is Role.SOURCE
                    and entity_token != admission.binding.source_entity_token
                    or expected.entity_token and entity_token != expected.entity_token):
                raise AdmissionConflict(Reason.BINDING_MISMATCH)
            # Accepted effects retain custody after expiry/revoke. Recording a
            # verified completion does not authorize another effect or unlock.
            conn.execute("UPDATE transfer_p2_steps SET phase=?,receipt=?,entity_token=? "
                         "WHERE operation_id=? AND generation=?",
                         (completed.phase, receipt, entity_token, operation_id, expected.generation))
            return completed

    def custody(self, actor: str, expected_entity_token: str) -> Custody:
        _, states = self.custody_snapshot(actor, expected_entity_token)
        return Custody(tuple(state.operation_id for state in states))

    def custody_snapshot(self, actor: str, expected_entity_token: str) -> tuple[int, tuple[StepState, ...]]:
        identifier(actor)
        identifier(expected_entity_token)
        with self._admission._database.read() as conn:
            conn.execute("BEGIN")  # revision and rows belong to one read snapshot
            revision = conn.execute("SELECT version FROM transfer_p2_revision WHERE singleton=1").fetchone()
            if revision is None or type(revision[0]) is not int or revision[0] < 0:
                raise AdmissionConflict(Reason.CORRUPT_RECORD)
            rows = conn.execute("SELECT DISTINCT s.operation_id FROM transfer_p2_steps s "
                                "JOIN transfer_p2_admissions a USING(operation_id) "
                                "WHERE s.actor=? AND a.role='source' "
                                "AND a.source_entity_token=? AND a.cancelled_at_ms IS NULL "
                                "ORDER BY s.operation_id", (actor, expected_entity_token)).fetchall()
            states = []
            for (op,) in rows:
                self._admission._require(conn, op)
                state = _read(conn, op)
                if state is None:
                    raise AdmissionConflict(Reason.CORRUPT_RECORD)
                states.append(state)
            return revision[0], tuple(states)

    def attention(self, operation_id: str, *, expected: StepState,
                  event_id: str, principal: str, action: str,
                  reason: str = "") -> StepState:
        """Audit an already authorized override; never execute or authorize it.

        ``reason`` is free text carried for the operator's audit trail. The
        string is never parsed as authority: the caller must have authenticated
        the principal before this row exists.
        """
        for value in (event_id, principal, action):
            identifier(value)
        if not isinstance(reason, str) or len(reason) > MAX_ATTENTION_REASON:
            raise ValueError("override reason must be a bounded string")
        if expected.operation_id != operation_id:
            raise AdmissionConflict(Reason.PHASE_CONFLICT)
        with self._admission._database.transaction() as conn:
            self._admission._require(conn, operation_id)
            current = _read(conn, operation_id)
            event = conn.execute("SELECT operation_id,generation,principal,action,reason "
                                 "FROM transfer_p2_attention WHERE event_id=?", (event_id,)).fetchone()
            if event is not None:
                if event != (operation_id, expected.generation, principal, action, reason):
                    raise AdmissionConflict(Reason.REPLAY_MISMATCH)
                return current
            if current != expected:
                raise AdmissionConflict(Reason.PHASE_CONFLICT)
            conn.execute("INSERT INTO transfer_p2_attention VALUES (?,?,?,?,?,?,?)",
                         (event_id, operation_id, expected.generation, principal,
                          action, reason, self._admission._clock()))
            conn.execute("UPDATE transfer_p2_steps SET needs_attention=1 "
                         "WHERE operation_id=? AND generation=?", (operation_id, expected.generation))
            return replace(current, needs_attention=True)

    def events(self, operation_id: str) -> tuple[AttentionEvent, ...]:
        with self._admission._database.read() as conn:
            self._admission._require(conn, operation_id)
            rows = conn.execute("SELECT * FROM transfer_p2_attention WHERE operation_id=? "
                                "ORDER BY recorded_at_ms,event_id", (operation_id,)).fetchall()
            return tuple(AttentionEvent(*row) for row in rows)

    def pending_quiesce(self, limit: int) -> tuple[StepState, ...]:
        """Bounded read of durable quiesce intents with no recorded result.

        Recovery uses these rows to re-derive state from owner receipts after a
        crash. A corrupt row is skipped, never repaired or unlocked here; the
        caller keeps fail-closed custody for anything it cannot certify.
        """

        if type(limit) is not int or limit < 1:
            raise ValueError("pending quiesce limit must be a positive int")
        with self._admission._database.read() as conn:
            rows = conn.execute(
                "SELECT operation_id, generation FROM transfer_p2_steps "
                "WHERE step='quiesce' AND phase='quiesce-pending' "
                "ORDER BY operation_id, generation LIMIT ?", (limit,)).fetchall()
            states = []
            for operation_id, generation in rows:
                try:
                    states.append(_read(conn, operation_id, generation))
                except AdmissionConflict:
                    continue
        return tuple(state for state in states if state is not None)

    def history(self, operation_id: str) -> tuple[StepState, ...]:
        """Retain each local intent/result; a newer step never overwrites it."""
        with self._admission._database.read() as conn:
            self._admission._require(conn, operation_id)
            rows = conn.execute("SELECT generation FROM transfer_p2_steps WHERE operation_id=? "
                                "ORDER BY generation", (operation_id,)).fetchall()
            return tuple(_read(conn, operation_id, generation) for (generation,) in rows)
