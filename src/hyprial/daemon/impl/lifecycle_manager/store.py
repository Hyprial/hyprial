"""The lifecycle journal store: reservations, effects and receipts."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from hyprial.daemon.impl.state_db  import StateDatabase
from hyprial.kernel  import (
    MutationProvenance,
)
from hyprial.kernel import DomainEffectClaim

from .steps import (
    _operation_from_json,
    _operation_json,
    _plan,
)
from .vocabulary import (
    LifecycleOperation,
    LifecycleOperationConflict,
    LifecycleResult,
    LifecycleState,
)


class _InjectedManagerCrash(BaseException):
    pass


_RECOVER_FAULT_EVENT_EVERY = 50


_RECOVER_FAULT_EVENT_MIN_INTERVAL_S = 30.0


def backfill_domain_attested_effects(
    state: Path | StateDatabase, claims: Iterable[DomainEffectClaim]
) -> tuple[str, ...]:
    """U0c startup: journal what the dead generation's receipts attest.

    A saga effect is durably complete in two places, in this order: the
    domain commits its mutation together with a receipt, then the process
    manager writes the journal completion.  A daemon death between the two
    leaves a receipt whose journal effect is still ``prepared``/
    ``dispatched`` -- the exact half-product U0c must clear.  Under the old
    resume semantics that window was healed by replaying the receipt
    forward; restarts no longer resume, so the receipt's attestation is
    instead BACKFILLED into the journal here, in one transaction, and the
    normal compensation machinery undoes it like any other completed
    effect.

    The claim's attempt token names the operation, the direction and the
    step ordinal (``<operation_id>:<direction>:<ordinal>``; parsed from
    the right, operation ids may contain colons); the step name comes from
    re-planning the journalled operation, exactly as ``_perform`` derived
    it.  Claims whose operation no longer exists (orphan receipts), whose
    token does not parse, or whose effect row is missing are skipped and
    left to the domain-local rollback in the receipt expiry -- they cannot
    be compensated through a saga that no longer exists.

    Returns the attempt tokens that were backfilled.  The journal tables
    are only ever UPDATEd here -- never pruned (the U0b retention
    tripwire guards exactly that).
    """

    store = _LifecycleStore(
        state if isinstance(state, StateDatabase) else StateDatabase(Path(state))
    )
    backfilled: list[str] = []
    with store._state_db.transaction() as db:
        for claim in claims:
            try:
                head, direction, ordinal_text = claim.attempt_token.rsplit(":", 2)
                ordinal = int(ordinal_text)
            except ValueError:
                continue
            if head != claim.operation_id or direction not in {
                "forward",
                "compensation",
            }:
                continue
            row = db.execute(
                "SELECT request_json FROM lifecycle_operations "
                "WHERE operation_id = ?",
                (claim.operation_id,),
            ).fetchone()
            if row is None:
                continue
            try:
                steps = _plan(_operation_from_json(str(row[0])))
            except (KeyError, TypeError, ValueError):
                continue
            if ordinal >= len(steps):
                continue
            name = steps[ordinal].name
            current = db.execute(
                "SELECT status FROM lifecycle_effects WHERE operation_id = ? "
                "AND effect_name = ? AND direction = ?",
                (claim.operation_id, name, direction),
            ).fetchone()
            if current is not None and str(current[0]) == "completed":
                continue
            cursor = db.execute(
                "UPDATE lifecycle_effects SET status = 'completed', "
                "correlation_id = ?, changed = ?, created_by_operation = ?, "
                "resource_token = ?, receipt_retired = 0 "
                "WHERE operation_id = ? AND effect_name = ? AND direction = ?",
                (
                    f"lifecycle:{claim.attempt_token}",
                    int(claim.changed),
                    int(claim.created_by_operation),
                    claim.resource_token,
                    claim.operation_id,
                    name,
                    direction,
                ),
            )
            if cursor.rowcount == 1:
                backfilled.append(claim.attempt_token)
    return tuple(backfilled)


@dataclass(frozen=True, slots=True)
class _EffectReceipt:
    correlation_id: str
    attempt_token: str


@dataclass(frozen=True, slots=True)
class _ReceiptRetirement:
    attempt_token: str
    resource_token: str


class _LifecycleStore:
    """The lifecycle journal over the shared state database (U0a-2 丙).

    Connections belong to ``StateDatabase``: one short-lived connection per
    transaction/read, write transactions serialized by the shared owner, so
    the former dedicated connection (and its contention with the
    desired-state store's writes) is gone by construction.
    """

    def __init__(self, state_db: StateDatabase) -> None:
        self._state_db = state_db
        with self._state_db.transaction(
            schema="""
            CREATE TABLE IF NOT EXISTS lifecycle_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_operations (
                operation_id TEXT PRIMARY KEY,
                request_digest TEXT NOT NULL,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                error_code TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_effects (
                operation_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                effect_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                attempt_token TEXT NOT NULL,
                status TEXT NOT NULL,
                changed INTEGER NOT NULL DEFAULT 1,
                created_by_operation INTEGER,
                resource_token TEXT,
                receipt_retired INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(operation_id, effect_name, direction)
            );
            """
        ) as db:
            columns = {
                str(row[1])
                for row in db.execute(
                    "PRAGMA table_info(lifecycle_operations)"
                )
            }
            if "error_code" not in columns:
                db.execute(
                    "ALTER TABLE lifecycle_operations ADD COLUMN error_code TEXT"
                )

    def next_generation(self) -> int:
        with self._state_db.transaction() as db:
            row = db.execute(
                "SELECT value FROM lifecycle_meta WHERE key = 'generation'"
            ).fetchone()
            value = (0 if row is None else int(row[0])) + 1
            db.execute(
                "INSERT INTO lifecycle_meta(key, value) VALUES('generation', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (value,),
            )
            return value

    def reserve(self, operation: LifecycleOperation) -> tuple[bool, LifecycleState]:
        payload = _operation_json(operation)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._state_db.transaction() as db:
            row = db.execute(
                "SELECT request_digest, state FROM lifecycle_operations WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()
            if row is not None:
                if str(row[0]) != digest:
                    raise LifecycleOperationConflict(
                        f"operationId {operation.operation_id!r} was reused with a different request"
                    )
                return False, LifecycleState(str(row[1]))
            db.execute(
                "INSERT INTO lifecycle_operations("
                "operation_id, request_digest, request_json, state, error, "
                "error_code, updated_at) VALUES(?, ?, ?, ?, NULL, NULL, ?)",
                (
                    operation.operation_id,
                    digest,
                    payload,
                    LifecycleState.RUNNING.value,
                    time.time(),
                ),
            )
        return True, LifecycleState.RUNNING

    def pending(self) -> tuple[str, ...]:
        with self._state_db.read() as db:
            rows = db.execute(
                "SELECT operation_id FROM lifecycle_operations "
                "WHERE state IN (?, ?) ORDER BY updated_at, operation_id",
                (LifecycleState.RUNNING.value, LifecycleState.COMPENSATING.value),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def interrupt_running_operations(
        self, cause: str, error_code: str
    ) -> tuple[str, ...]:
        """U0c: a saga that missed its generation is compensated, not resumed.

        Called exactly once per process-manager construction, BEFORE the
        worker thread exists (so nothing of the new generation has touched
        the journal yet and every RUNNING row belongs to a dead daemon
        generation).  The row's completion status stops being a resume
        cursor at the generation boundary: the journal crosses generations
        only as compensation input (which resources the operation touched),
        never as forward progress.  Rows already COMPENSATING keep their
        original cause -- finishing an interrupted teardown is itself
        compensation, so those operations are left exactly as they are.
        """

        with self._state_db.transaction() as db:
            rows = db.execute(
                "SELECT operation_id FROM lifecycle_operations "
                "WHERE state = ? ORDER BY updated_at, operation_id",
                (LifecycleState.RUNNING.value,),
            ).fetchall()
            interrupted = tuple(str(row[0]) for row in rows)
            if interrupted:
                db.execute(
                    "UPDATE lifecycle_operations SET state = ?, error = ?, "
                    "error_code = ?, updated_at = ? WHERE state = ?",
                    (
                        LifecycleState.COMPENSATING.value,
                        cause,
                        error_code,
                        time.time(),
                        LifecycleState.RUNNING.value,
                    ),
                )
        return interrupted

    def load(self, operation_id: str) -> LifecycleOperation:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT request_json FROM lifecycle_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _operation_from_json(str(row[0]))

    def state(self, operation_id: str) -> LifecycleState:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT state FROM lifecycle_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return LifecycleState(str(row[0]))

    def set_state(
        self,
        operation_id: str,
        state: LifecycleState,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._state_db.transaction() as db:
            db.execute(
                "UPDATE lifecycle_operations SET state = ?, error = ?, "
                "error_code = ?, updated_at = ? "
                "WHERE operation_id = ?",
                (state.value, error, error_code, time.time(), operation_id),
            )

    def effect_done(self, operation_id: str, name: str, direction: str) -> bool:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT status FROM lifecycle_effects WHERE operation_id = ? "
                "AND effect_name = ? AND direction = ?",
                (operation_id, name, direction),
            ).fetchone()
        return bool(row is not None and row[0] == "completed")

    def prepare_effect(
        self,
        operation_id: str,
        ordinal: int,
        name: str,
        direction: str,
        correlation_id: str,
        attempt_token: str,
    ) -> _EffectReceipt:
        """Durably freeze attempt identity before the first domain admission."""

        with self._state_db.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO lifecycle_effects("
                "operation_id, ordinal, effect_name, direction, correlation_id, "
                "attempt_token, status, changed, created_by_operation, resource_token"
                ", receipt_retired) "
                "VALUES(?, ?, ?, ?, ?, ?, 'prepared', 0, NULL, NULL, 0)",
                (
                    operation_id,
                    ordinal,
                    name,
                    direction,
                    correlation_id,
                    attempt_token,
                ),
            )
            row = db.execute(
                "SELECT correlation_id, attempt_token "
                "FROM lifecycle_effects WHERE operation_id = ? "
                "AND effect_name = ? AND direction = ?",
                (operation_id, name, direction),
            ).fetchone()
        if row is None:
            raise RuntimeError("effect ownership receipt was not persisted")
        return _EffectReceipt(str(row[0]), str(row[1]))

    def effect_receipt(
        self, operation_id: str, name: str, direction: str
    ) -> _EffectReceipt | None:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT correlation_id, attempt_token "
                "FROM lifecycle_effects WHERE operation_id = ? "
                "AND effect_name = ? AND direction = ?",
                (operation_id, name, direction),
            ).fetchone()
        if row is None:
            return None
        return _EffectReceipt(str(row[0]), str(row[1]))

    def mark_dispatched(self, operation_id: str, name: str, direction: str) -> None:
        """Advance an unresolved effect to 'dispatched' -- never backwards.

        ⚠️ The ``AND status != 'completed'`` clause is NOT just an
        idempotency guard: it carries the U0b migration's rescue.  That
        rescue (``desired_state_sqlite.py`` ``_journal_proved_running``,
        see its ⚠️ POSITIONAL DEPENDENCY note) proves a harness row once
        ran by reading completed forward ``harness.ensure`` effects --
        so a completed effect must never be demoted, by this method or
        any other writer.  Relaxing this clause would silently destroy
        that evidence, and the retention tripwire
        (tests/daemon/lifecycle/journal/test_lifecycle_journal_retention_gate.py) only scans
        DELETE FROM / DROP TABLE -- it cannot catch an UPDATE-based
        relaxation.  The same one-way rule governs every status writer:
        ``complete_effect`` and the U0c ``backfill_domain_attested_effects``
        only move effects toward 'completed'.
        """

        with self._state_db.transaction() as db:
            db.execute(
                "UPDATE lifecycle_effects SET status = 'dispatched' "
                "WHERE operation_id = ? AND effect_name = ? AND direction = ? "
                "AND status != 'completed'",
                (operation_id, name, direction),
            )

    def complete_effect(
        self,
        operation_id: str,
        name: str,
        direction: str,
        correlation_id: str,
        *,
        provenance: MutationProvenance,
    ) -> None:
        with self._state_db.transaction() as db:
            cursor = db.execute(
                "UPDATE lifecycle_effects SET status = 'completed', "
                "correlation_id = ?, changed = ?, created_by_operation = ?, "
                "resource_token = ?, receipt_retired = 0 "
                "WHERE operation_id = ? AND effect_name = ? AND direction = ?",
                (
                    correlation_id,
                    int(provenance.changed),
                    int(provenance.created_by_operation),
                    provenance.resource_token,
                    operation_id,
                    name,
                    direction,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("completed effect has no ownership receipt")
            db.execute(
                "UPDATE lifecycle_operations SET updated_at = ? WHERE operation_id = ?",
                (time.time(), operation_id),
            )

    def receipt_retirement(
        self, operation_id: str, name: str, direction: str
    ) -> _ReceiptRetirement | None:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT attempt_token, resource_token FROM lifecycle_effects "
                "WHERE operation_id = ? AND effect_name = ? AND direction = ? "
                "AND status = 'completed' AND receipt_retired = 0",
                (operation_id, name, direction),
            ).fetchone()
        if row is None:
            return None
        if row[1] is None:
            raise RuntimeError("completed effect lacks a resource token")
        return _ReceiptRetirement(str(row[0]), str(row[1]))

    def completed_receipt(
        self, operation_id: str, name: str, direction: str
    ) -> _ReceiptRetirement:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT attempt_token, resource_token FROM lifecycle_effects "
                "WHERE operation_id = ? AND effect_name = ? AND direction = ? "
                "AND status = 'completed'",
                (operation_id, name, direction),
            ).fetchone()
        if row is None or row[1] is None:
            raise RuntimeError("completed effect lacks receipt identity")
        return _ReceiptRetirement(str(row[0]), str(row[1]))

    def mark_receipt_retired(
        self, operation_id: str, name: str, direction: str
    ) -> None:
        with self._state_db.transaction() as db:
            cursor = db.execute(
                "UPDATE lifecycle_effects SET receipt_retired = 1 "
                "WHERE operation_id = ? AND effect_name = ? AND direction = ? "
                "AND status = 'completed'",
                (operation_id, name, direction),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("cannot retire a non-completed effect receipt")

    def forward_resource_token(self, operation_id: str, name: str) -> str:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT resource_token FROM lifecycle_effects "
                "WHERE operation_id = ? AND effect_name = ? "
                "AND direction = 'forward' AND status = 'completed'",
                (operation_id, name),
            ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError(f"forward effect {name!r} has no resource token")
        return str(row[0])

    def completed_forward(self, operation_id: str) -> tuple[str, ...]:
        with self._state_db.read() as db:
            rows = db.execute(
                "SELECT effect_name FROM lifecycle_effects WHERE operation_id = ? "
                "AND direction = 'forward' AND status = 'completed' ORDER BY ordinal",
                (operation_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def compensable_forward(self, operation_id: str) -> tuple[str, ...]:
        with self._state_db.read() as db:
            rows = db.execute(
                "SELECT effect_name FROM lifecycle_effects WHERE operation_id = ? "
                "AND direction = 'forward' AND status = 'completed' "
                "AND created_by_operation = 1 "
                "ORDER BY ordinal",
                (operation_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def result(self, operation_id: str) -> LifecycleResult:
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT state, error, error_code FROM lifecycle_operations "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            effects = db.execute(
                "SELECT effect_name, direction FROM lifecycle_effects "
                "WHERE operation_id = ? AND status = 'completed' ORDER BY ordinal",
                (operation_id,),
            ).fetchall()
        if row is None:
            raise KeyError(operation_id)
        return LifecycleResult(
            operation_id,
            LifecycleState(str(row[0])),
            tuple(str(item[0]) for item in effects if item[1] == "forward"),
            tuple(str(item[0]) for item in effects if item[1] == "compensation"),
            None if row[1] is None else str(row[1]),
            None if row[2] is None else str(row[2]),
        )

    def close(self) -> None:
        # Connections live one transaction/read (StateDatabase); nothing
        # persistent to close.  Kept as a no-op for the manager's drain path.
        return None
