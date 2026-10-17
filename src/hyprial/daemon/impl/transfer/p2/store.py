"""Preparation ledger in the daemon's shared StateDatabase.

An admission reserves local custody but does not stop, create or launch agents.
Expiry/revocation never releases a reservation. Only an explicit cancellation of
this preparation-only record can do so -- which now includes the successor of an
operation an authenticated principal already audited away (``_reap_audited_away``:
the audit row is that explicit act, and without it the worker stayed reserved
forever after one failed transfer). Later phase progression must replace that
restriction with phase-aware fencing before enabling any side effects.
"""

from collections.abc import Callable
from dataclasses import replace
import sqlite3
import time

from hyprial.daemon.impl.state_db import StateDatabase

from .journal import PhaseJournal, SCHEMA as JOURNAL_SCHEMA
from .model import Admission, AdmissionConflict, Binding, Reason, Role, identifier

ADMISSION_TTL_MS = 15 * 60 * 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_p2_admissions (
    operation_id TEXT PRIMARY KEY,
    binding TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('source', 'target')),
    owner TEXT NOT NULL,
    source_node TEXT NOT NULL,
    source_agent_id TEXT NOT NULL,
    source_entity_token TEXT NOT NULL,
    target_node TEXT NOT NULL,
    target_actor TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    cancelled_at_ms INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS transfer_p2_source_custody
ON transfer_p2_admissions(owner, source_node, source_agent_id, source_entity_token)
WHERE role = 'source' AND cancelled_at_ms IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS transfer_p2_target_name
ON transfer_p2_admissions(target_node, target_actor)
WHERE role = 'target' AND cancelled_at_ms IS NULL;
"""
_SELECT = """SELECT operation_id, binding, created_at_ms, expires_at_ms,
revoked_at_ms, cancelled_at_ms FROM transfer_p2_admissions WHERE operation_id = ?"""


def _read(conn: sqlite3.Connection, operation_id: str) -> Admission | None:
    row = conn.execute(_SELECT, (operation_id,)).fetchone()
    return None if row is None else Admission(row[0], Binding.decode(row[1]), *row[2:])


class AdmissionStore:
    """Persistence used only by the P2 actor's single effect writer.

SQLite constraints additionally protect offline/restart races. Caller admission
is deliberately not inferred from fields in Binding: no public IPC uses this
store until the target-local trusted-operator adapter is implemented.
"""

    def __init__(self, database: StateDatabase, *, node: str, owner: str,
                 clock_ms: Callable[[], int] | None = None) -> None:
        identifier(node)
        identifier(owner)
        self._database = database
        self._node = node
        self._owner = owner
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        with database.transaction(_SCHEMA + JOURNAL_SCHEMA):
            pass
        self._migrate_attention_reason(database)
        self.journal = PhaseJournal(self)

    @staticmethod
    def _migrate_attention_reason(database: StateDatabase) -> None:
        """Add the audit reason column to a journal created before it existed.

        ``CREATE TABLE IF NOT EXISTS`` cannot widen an existing table, and the
        C1 table is already deployed on integration nodes. The added column has
        a default, so pre-existing audit rows stay readable.
        """

        with database.read() as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(transfer_p2_attention)")
            }
        if "reason" in columns:
            return
        with database.transaction(
            "ALTER TABLE transfer_p2_attention "
            "ADD COLUMN reason TEXT NOT NULL DEFAULT '';"
        ):
            pass

    def _check_binding(self, binding: Binding) -> None:
        local = binding.source_node if binding.role is Role.SOURCE else binding.target_node
        if (binding.owner != self._owner or local != self._node
                or binding.source_node == binding.target_node):
            raise AdmissionConflict(Reason.BINDING_MISMATCH)

    def _require(self, conn: sqlite3.Connection, operation_id: str) -> Admission:
        record = _read(conn, operation_id)
        if record is None:
            raise AdmissionConflict(Reason.UNKNOWN)
        self._check_binding(record.binding)
        return record

    def prepare(self, operation_id: str, binding: Binding) -> Admission:
        identifier(operation_id)
        self._check_binding(binding)
        with self._database.transaction() as conn:
            existing = _read(conn, operation_id)
            if existing is not None:
                if existing.binding != binding:
                    raise AdmissionConflict(Reason.REPLAY_MISMATCH)
                return existing
            now = self._clock()
            record = Admission(operation_id, binding, now, now + ADMISSION_TTL_MS, None, None)
            for attempt in (1, 2):
                try:
                    conn.execute(
                        """INSERT INTO transfer_p2_admissions VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                        (operation_id, binding.encode(), binding.role, binding.owner,
                         binding.source_node, binding.source_agent_id, binding.source_entity_token,
                         binding.target_node, binding.target_actor, now, record.expires_at_ms),
                    )
                except sqlite3.IntegrityError as error:
                    # Only uniqueness is a competing reservation; schema/data
                    # corruption must remain loud instead of masquerading as busy.
                    if error.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                        raise
                    if attempt == 1 and self._reap_audited_away(conn, binding, now):
                        continue
                    raise AdmissionConflict(Reason.RESERVED) from error
                return record
            raise AdmissionConflict(Reason.RESERVED)

    def _reap_audited_away(
        self, conn: sqlite3.Connection, binding: Binding, now: int
    ) -> bool:
        """Free the slot of a competing preparation an operator audited away.

        A failed transfer leaves its preparation in place; the rollback then
        records the audited override that released the hold (principal, reason,
        journal event) but cannot delete the reservation, so the next attempt
        used to hit the unique index and answer ``reserved`` forever -- a real
        two-daemon probe ended in exactly that wedge, with no way out.

        The audit row is the evidence: when every step of the competing
        operation is ``needs_attention``, an authenticated principal has
        already released what that preparation was holding, so the successor
        may take the slot.  Nothing else is reaped -- a preparation with an
        un-audited step, or with no step at all, proves nothing about what it
        may still do, and keeps its reservation (C1: expiry and revocation
        never release one by themselves).
        """

        if binding.role is not Role.SOURCE:
            return False
        rows = conn.execute(
            "SELECT operation_id FROM transfer_p2_admissions WHERE owner=? AND "
            "source_node=? AND source_agent_id=? AND source_entity_token=? AND "
            "role='source' AND cancelled_at_ms IS NULL",
            (binding.owner, binding.source_node, binding.source_agent_id,
             binding.source_entity_token),
        ).fetchall()
        for (operation_id,) in rows:
            steps = conn.execute(
                "SELECT needs_attention FROM transfer_p2_steps WHERE operation_id=?",
                (operation_id,),
            ).fetchall()
            if steps and all(bool(step[0]) for step in steps):
                conn.execute(
                    "UPDATE transfer_p2_admissions SET cancelled_at_ms=? "
                    "WHERE operation_id=?",
                    (now, operation_id),
                )
                return True
        return False

    def get(self, operation_id: str) -> Admission | None:
        identifier(operation_id)
        with self._database.read() as conn:
            record = _read(conn, operation_id)
            if record is not None:
                self._check_binding(record.binding)
            return record

    def require_live(self, operation_id: str) -> Admission:
        with self._database.read() as conn:
            record = self._require(conn, operation_id)
            record.require_live(self._clock())
            return record

    def renew(self, operation_id: str, *, expected_expiry: int,
              expires_at_ms: int) -> Admission:
        if type(expected_expiry) is not int or type(expires_at_ms) is not int:
            raise AdmissionConflict(Reason.RENEWAL_CONFLICT)
        with self._database.transaction() as conn:
            record = self._require(conn, operation_id)
            if record.cancelled_at_ms is not None:
                raise AdmissionConflict(Reason.CANCELLED)
            if record.revoked_at_ms is not None:
                raise AdmissionConflict(Reason.REVOKED)
            now = self._clock()
            if now >= record.expires_at_ms:
                raise AdmissionConflict(Reason.EXPIRED)
            if record.expires_at_ms == expires_at_ms:
                return record
            if (record.expires_at_ms != expected_expiry
                    or expires_at_ms <= max(now, record.expires_at_ms)
                    or expires_at_ms > now + ADMISSION_TTL_MS):
                raise AdmissionConflict(Reason.RENEWAL_CONFLICT)
            conn.execute("UPDATE transfer_p2_admissions SET expires_at_ms=? WHERE operation_id=?",
                         (expires_at_ms, operation_id))
            return replace(record, expires_at_ms=expires_at_ms)

    def revoke(self, operation_id: str) -> Admission:
        with self._database.transaction() as conn:
            record = self._require(conn, operation_id)
            if record.revoked_at_ms is not None or record.cancelled_at_ms is not None:
                return record
            now = self._clock()
            conn.execute("UPDATE transfer_p2_admissions SET revoked_at_ms=? WHERE operation_id=?",
                         (now, operation_id))
            return replace(record, revoked_at_ms=now)

    def source_custody(self, *, owner: str, node: str, agent_id: str,
                       entity_token: str) -> Admission | None:
        """The live source preparation that reserves one incarnation, if any.

        Read-only: the caller uses it to say WHICH operation holds a worker, so
        a refusal names the release instead of repeating ``reserved``.
        """

        for value in (owner, node, agent_id, entity_token):
            identifier(value)
        with self._database.read() as conn:
            row = conn.execute(
                "SELECT operation_id FROM transfer_p2_admissions WHERE owner=? "
                "AND source_node=? AND source_agent_id=? AND source_entity_token=? "
                "AND role='source' AND cancelled_at_ms IS NULL",
                (owner, node, agent_id, entity_token),
            ).fetchone()
            return None if row is None else self._require(conn, row[0])

    def cancel_prepared(self, operation_id: str) -> Admission:
        """Release a preparation which cannot yet have external side effects.

        This is NOT a transfer rollback or evidence that a worker was fenced.
        """
        with self._database.transaction() as conn:
            record = self._require(conn, operation_id)
            if record.cancelled_at_ms is not None:
                return record
            if conn.execute("SELECT 1 FROM transfer_p2_steps WHERE operation_id=?",
                            (operation_id,)).fetchone() is not None:
                raise AdmissionConflict(Reason.PHASE_CONFLICT)
            now = self._clock()
            conn.execute("UPDATE transfer_p2_admissions SET cancelled_at_ms=? WHERE operation_id=?",
                         (now, operation_id))
            return replace(record, cancelled_at_ms=now)
