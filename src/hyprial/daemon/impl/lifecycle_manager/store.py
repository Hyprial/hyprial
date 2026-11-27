"""The lifecycle journal store: reservations, effects and receipts."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import hashlib
import json
import logging
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
    CELL_MEMBER_EFFECT_NAMES,
    CELL_MEMBER_EFFECT_PREFIX,
    LegacyLifecycleOperationError,
    _operation_from_json,
    _operation_json,
    _plan,
    cell_member_effect_operation_id,
    is_cell_member_effect_operation,
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
_LOGGER = logging.getLogger("hyprial.daemon.impl.lifecycle_manager")


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
        """Incomplete lifecycle sagas this journal's manager drives.

        Cell member reconcile effects share the table but are owned and
        settled by the cell reconcile effect port, never by a lifecycle
        manager, so they are not part of the manager's work.
        """

        with self._state_db.read() as db:
            rows = db.execute(
                "SELECT operation_id FROM lifecycle_operations "
                "WHERE state IN (?, ?) AND substr(operation_id, 1, ?) != ? "
                "ORDER BY updated_at, operation_id",
                (
                    LifecycleState.RUNNING.value,
                    LifecycleState.COMPENSATING.value,
                    len(CELL_MEMBER_EFFECT_PREFIX),
                    CELL_MEMBER_EFFECT_PREFIX,
                ),
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

        A cell member reconcile effect left incomplete by a dead generation
        is not a saga: its fence epoch died with the reconcile actor, so it
        is failed closed here and never handed to compensation.
        """

        now = time.time()
        with self._state_db.transaction() as db:
            rows = db.execute(
                "SELECT operation_id, state FROM lifecycle_operations "
                "WHERE state IN (?, ?) ORDER BY updated_at, operation_id",
                (LifecycleState.RUNNING.value, LifecycleState.COMPENSATING.value),
            ).fetchall()
            interrupted: list[str] = []
            for operation_id, state in rows:
                op_id = str(operation_id)
                if is_cell_member_effect_operation(op_id):
                    db.execute(
                        "UPDATE lifecycle_operations SET state = ?, error = ?, "
                        "error_code = ?, updated_at = ? WHERE operation_id = ?",
                        (
                            LifecycleState.FAILED.value,
                            "cell reconcile effect did not finish in its daemon generation",
                            "LIFECYCLE_FAIL_CLOSED",
                            now,
                            op_id,
                        ),
                    )
                    continue
                if str(state) != LifecycleState.RUNNING.value:
                    continue
                db.execute(
                    "UPDATE lifecycle_operations SET state = ?, error = ?, "
                    "error_code = ?, updated_at = ? WHERE operation_id = ?",
                    (LifecycleState.COMPENSATING.value, cause, error_code, now, op_id),
                )
                interrupted.append(op_id)
        return tuple(interrupted)

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

    def read_start_failures(self):
        failures, _ = self.read_runtime_receipts()
        return failures

    def read_runtime_receipts(self):
        """Latest reservation per actor, so a subsequent start invalidates a stop."""
        with self._state_db.read() as db:
            rows = db.execute(
                "SELECT request_json, error, updated_at, state FROM ("
                "SELECT *, ROW_NUMBER() OVER (PARTITION BY "
                "json_extract(request_json, '$.target.actor') ORDER BY rowid DESC) AS position "
                "FROM lifecycle_operations WHERE json_valid(request_json) = 1) WHERE position = 1 "
                "AND ((state = 'completed' AND json_extract(request_json, '$.kind') = 'deactivate') "
                "OR (state = 'compensated' AND error_code = 'HARNESS_START_FAILED' "
                "AND json_extract(request_json, '$.kind') IN ('create', 'resume')))"
            ).fetchall()
            result = {}
            stops = {}
            for payload, error, updated_at, state in rows:
                try:
                    operation = _operation_from_json(payload)
                except LegacyLifecycleOperationError as legacy_error:
                    _LOGGER.warning(
                        "skipping unreadable legacy start-failure projection (%s): %s",
                        type(legacy_error).__name__,
                        legacy_error,
                    )
                    continue
                if state == "completed":
                    stops[operation.target.actor] = operation.operation_id
                elif operation.observed_entity_token is not None:
                    result[operation.target.actor] = (
                        operation, str(error or ""), int(updated_at * 1000),
                    )
            return result, stops

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
        return None

    def record_cell_member_effect(
        self,
        *,
        cell_id: str,
        epoch: str,
        revision: int,
        member: str,
        effect_name: str,
        correlation_id: str,
        attempt_token: str,
        ordinal: int = 0,
    ) -> _EffectReceipt:
        """Persist a fenced I6 cell member effect without reusing ``harness.ensure``."""

        if effect_name not in CELL_MEMBER_EFFECT_NAMES:
            raise ValueError(f"unsupported cell member effect {effect_name!r}")
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError("cell member effect requires non-empty correlation_id")
        if not isinstance(attempt_token, str) or not attempt_token.strip():
            raise ValueError("cell member effect requires non-empty attempt_token")
        operation_id = cell_member_effect_operation_id(
            cell_id, epoch, revision, member, effect_name
        )
        request_payload = json.dumps(
            {
                "cellId": cell_id,
                "epoch": epoch,
                "revision": revision,
                "member": member,
                "effectName": effect_name,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._state_db.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO lifecycle_operations("
                "operation_id, request_digest, request_json, state, error, "
                "error_code, updated_at) VALUES(?, ?, ?, ?, NULL, NULL, ?)",
                (
                    operation_id,
                    hashlib.sha256(operation_id.encode("utf-8")).hexdigest(),
                    request_payload,
                    LifecycleState.RUNNING.value,
                    time.time(),
                ),
            )
            existing = db.execute(
                "SELECT correlation_id, attempt_token, status "
                "FROM lifecycle_effects WHERE operation_id = ? "
                "AND effect_name = ? AND direction = 'forward'",
                (operation_id, effect_name),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO lifecycle_effects("
                    "operation_id, ordinal, effect_name, direction, correlation_id, "
                    "attempt_token, status, changed, created_by_operation, "
                    "resource_token, receipt_retired) "
                    "VALUES(?, ?, ?, 'forward', ?, ?, 'prepared', 0, NULL, NULL, 0)",
                    (
                        operation_id,
                        ordinal,
                        effect_name,
                        correlation_id,
                        attempt_token,
                    ),
                )
            elif str(existing[2]) in {"failed", "completed"} and str(existing[1]) != attempt_token:
                db.execute(
                    "UPDATE lifecycle_effects SET ordinal = ?, correlation_id = ?, "
                    "attempt_token = ?, status = 'prepared', changed = 0, "
                    "created_by_operation = NULL, resource_token = NULL "
                    "WHERE operation_id = ? AND effect_name = ? AND direction = 'forward' "
                    "AND status IN ('failed', 'completed')",
                    (ordinal, correlation_id, attempt_token, operation_id, effect_name),
                )
                db.execute(
                    "UPDATE lifecycle_operations SET state = ?, error = NULL, "
                    "error_code = NULL, updated_at = ? WHERE operation_id = ?",
                    (LifecycleState.RUNNING.value, time.time(), operation_id),
                )
            row = db.execute(
                "SELECT correlation_id, attempt_token "
                "FROM lifecycle_effects WHERE operation_id = ? "
                "AND effect_name = ? AND direction = 'forward'",
                (operation_id, effect_name),
            ).fetchone()
        if row is None:
            raise RuntimeError("cell member effect receipt was not persisted")
        return _EffectReceipt(str(row[0]), str(row[1]))

    def complete_cell_member_effect(
        self,
        *,
        cell_id: str,
        epoch: str,
        revision: int,
        member: str,
        effect_name: str,
        correlation_id: str,
        succeeded: bool,
        attempt_token: str | None = None,
        resource_token: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Commit terminal outcome for one fenced cell member effect."""

        if effect_name not in CELL_MEMBER_EFFECT_NAMES:
            raise ValueError(f"unsupported cell member effect {effect_name!r}")
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError("cell member effect requires non-empty correlation_id")
        if not isinstance(attempt_token, str) or not attempt_token.strip():
            raise ValueError("cell member effect requires non-empty attempt_token")
        operation_id = cell_member_effect_operation_id(
            cell_id, epoch, revision, member, effect_name
        )
        token = resource_token or correlation_id
        with self._state_db.transaction() as db:
            receipt_row = db.execute(
                "SELECT correlation_id, attempt_token, status "
                "FROM lifecycle_effects WHERE operation_id = ? "
                "AND effect_name = ? AND direction = 'forward'",
                (operation_id, effect_name),
            ).fetchone()
            if receipt_row is None:
                raise RuntimeError("cell member effect has no ownership receipt")
            if str(receipt_row[0]) != correlation_id or str(receipt_row[1]) != attempt_token:
                raise RuntimeError("cell member effect receipt mismatch")
            if str(receipt_row[2]) == "completed":
                raise RuntimeError("cell member effect is already completed")
            if succeeded:
                cursor = db.execute(
                    "UPDATE lifecycle_effects SET status = 'completed', "
                    "changed = 1, created_by_operation = 1, "
                    "resource_token = ?, receipt_retired = 0 "
                    "WHERE operation_id = ? AND effect_name = ? AND direction = 'forward' "
                    "AND correlation_id = ? AND attempt_token = ? AND status != 'completed'",
                    (token, operation_id, effect_name, correlation_id, attempt_token),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("cell member effect has no ownership receipt")
                db.execute(
                    "UPDATE lifecycle_operations SET state = ?, error = NULL, "
                    "error_code = NULL, updated_at = ? WHERE operation_id = ?",
                    (LifecycleState.COMPLETED.value, time.time(), operation_id),
                )
            else:
                cursor = db.execute(
                    "UPDATE lifecycle_effects SET status = 'failed' "
                    "WHERE operation_id = ? AND effect_name = ? AND direction = 'forward' "
                    "AND correlation_id = ? AND attempt_token = ? AND status != 'completed'",
                    (operation_id, effect_name, correlation_id, attempt_token),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("cell member effect has no ownership receipt")
                db.execute(
                    "UPDATE lifecycle_operations SET state = ?, error = ?, "
                    "error_code = ?, updated_at = ? WHERE operation_id = ?",
                    (
                        LifecycleState.FAILED.value,
                        error,
                        error_code,
                        time.time(),
                        operation_id,
                    ),
                )

    def cell_member_effect_status(
        self,
        *,
        cell_id: str,
        epoch: str,
        revision: int,
        member: str,
        effect_name: str,
    ) -> str | None:
        """Read the persisted status for one fenced cell member effect."""

        operation_id = cell_member_effect_operation_id(
            cell_id, epoch, revision, member, effect_name
        )
        with self._state_db.read() as db:
            row = db.execute(
                "SELECT status FROM lifecycle_effects "
                "WHERE operation_id = ? AND effect_name = ? AND direction = 'forward'",
                (operation_id, effect_name),
            ).fetchone()
        return None if row is None else str(row[0])

    def fail_closed_legacy_operations(self) -> tuple[str, ...]:
        """Mark unparseable incomplete operations failed instead of guessing."""

        with self._state_db.transaction() as db:
            rows = db.execute(
                "SELECT operation_id, request_json FROM lifecycle_operations "
                "WHERE state IN (?, ?)",
                (LifecycleState.RUNNING.value, LifecycleState.COMPENSATING.value),
            ).fetchall()
            failed: list[str] = []
            for operation_id, request_json in rows:
                op_id = str(operation_id)
                if is_cell_member_effect_operation(op_id):
                    continue
                try:
                    _operation_from_json(str(request_json))
                except LegacyLifecycleOperationError as error:
                    db.execute(
                        "UPDATE lifecycle_operations SET state = ?, error = ?, "
                        "error_code = ?, updated_at = ? WHERE operation_id = ?",
                        (
                            LifecycleState.FAILED.value,
                            str(error),
                            "LEGACY_OPERATION_UNSUPPORTED",
                            time.time(),
                            op_id,
                        ),
                    )
                    failed.append(op_id)
        return tuple(failed)
