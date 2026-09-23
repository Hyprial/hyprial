"""Durable lifecycle saga over Agent, Session, Harness and Route ports.

The process manager is application-core coordination, not a business actor.  It
owns no domain state and imports no actor backend.  Every cross-domain mutation
is a typed command followed by a correlated completion; completed effects are
journalled before the next step and compensation is the reverse of that same
plan.  Transfer therefore reuses the identical saga engine.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Callable, Literal
from collections.abc import Iterable

from hyprial.agents.ports import (
    AgentMutationCompleted,
    BindAgentCommand,
    CreateAgentCommand,
    DestroyAgentCommand,
    ReleaseAgentCommand,
)
from hyprial.contracts.ports import PortAdmission, PortCommandRejected

from hyprial.contracts.lifecycle_budgets import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS, LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from .correlation import (
    CorrelatedEvent,
    CorrelationEventRouter,
    CorrelationRouterOverloaded,
)
from .harness_ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
    HarnessMutationCompleted,
    RemoveHarnessCommand,
)
from .state_db import StateDatabase
from .lifecycle_receipts import (
    AgentLifecycleMutationPort,
    AtomicLifecycleMutationPort,
    DomainEffectClaim,
    HarnessLifecycleMutationPort,
    LifecycleMutationCompleted,
    LifecycleMutationFailed,
    LifecycleMutationRequest,
    MutationProvenance,
    RouteLifecycleMutationPort,
    SessionLifecycleMutationPort,
)
from .route_ports import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteMutationFailed,
    RouteSpec,
)
from .session_ports import (
    RegisterSessionCommand,
    SessionMutationCompleted,
    UnregisterSessionCommand,
)


class LifecycleKind(StrEnum):
    CREATE = "create"
    DEACTIVATE = "deactivate"
    REMOVE = "remove"
    TRANSFER = "transfer"


class LifecycleState(StrEnum):
    RUNNING = "running"
    COMPENSATING = "compensating"
    COMPLETED = "completed"
    COMPENSATED = "compensated"
    FAILED = "failed"


# Lifecycle operation budgets live in the contract layer so the CLI can size
# its down IPC wait without importing this (daemon) module; re-exported here
# for the manager and application. Card 104164aa (c).



@dataclass(frozen=True, slots=True)
class SessionLifecycleSpec:
    cwd: str
    command: tuple[str, ...]
    source: str
    session_ref: str
    runtime: str = "claude_interactive"


@dataclass(frozen=True, slots=True)
class LifecycleSpec:
    agent_name: str
    actor: str
    harness: HarnessLaunchProjection
    route: RouteSpec
    session: SessionLifecycleSpec | None = None


@dataclass(frozen=True, slots=True)
class LifecycleOperation:
    operation_id: str
    kind: LifecycleKind
    target: LifecycleSpec
    source: LifecycleSpec | None = None

    @classmethod
    def create(cls, operation_id: str, target: LifecycleSpec) -> LifecycleOperation:
        return cls(operation_id, LifecycleKind.CREATE, target)

    @classmethod
    def remove(cls, operation_id: str, target: LifecycleSpec) -> LifecycleOperation:
        return cls(operation_id, LifecycleKind.REMOVE, target)

    @classmethod
    def deactivate(
        cls, operation_id: str, target: LifecycleSpec
    ) -> LifecycleOperation:
        """Stop one runtime while preserving the durable Agent entity."""

        return cls(operation_id, LifecycleKind.DEACTIVATE, target)

    @classmethod
    def transfer(
        cls,
        operation_id: str,
        *,
        source: LifecycleSpec,
        target: LifecycleSpec,
    ) -> LifecycleOperation:
        return cls(operation_id, LifecycleKind.TRANSFER, target, source)


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    operation_id: str
    state: LifecycleState
    completed_effects: tuple[str, ...]
    compensated_effects: tuple[str, ...]
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LifecyclePorts:
    agent: AgentLifecycleMutationPort
    session: SessionLifecycleMutationPort
    harness: HarnessLifecycleMutationPort
    route: RouteLifecycleMutationPort


class LifecycleDomainPort:
    """Composition adapter for a receipt-producing typed domain port.

    The wrapped domain, not this adapter, must commit
    :class:`LifecycleMutationCompleted` provenance in the same transaction as
    its mutation.  This class intentionally has no projection/pre-read hook.
    """

    def __init__(
        self,
        domain: Literal["agent", "session", "harness"],
        commands: AtomicLifecycleMutationPort,
        *,
        generation: Callable[[], int],
        version: Callable[[], int],
        retire_receipt: Callable[[str, str], bool],
        confirm_receipt_retired: Callable[[str, str], None],
    ) -> None:
        self.domain = domain
        self._commands = commands
        self._generation = generation
        self._version = version
        self._retire_receipt = retire_receipt
        self._confirm_receipt_retired = confirm_receipt_retired

    @property
    def generation(self) -> int:
        return self._generation()

    @property
    def version(self) -> int:
        return self._version()

    def retire_receipt(self, attempt_token: str, resource_token: str) -> bool:
        return self._retire_receipt(attempt_token, resource_token)

    def confirm_receipt_retired(self, attempt_token: str, resource_token: str) -> None:
        self._confirm_receipt_retired(attempt_token, resource_token)

    def submit(self, command: LifecycleMutationRequest) -> PortAdmission:
        return self._commands.submit(command)


@dataclass(frozen=True, slots=True)
class _Step:
    name: str
    domain: str
    spec: LifecycleSpec
    forward: str
    inverse: str
    route_owner: str | None = None


class _InjectedManagerCrash(BaseException):
    pass


#: Recover-fault event throttle: the FIRST fault is always emitted, then one
#: every N consecutive faults, each carrying the running count; a "recovered"
#: event closes the episode.  A count, not a clock -- a locked database under
#: contention must not turn into a log flood of one event per retry.
_RECOVER_FAULT_EVENT_EVERY = 50

#: ...and a clock floor on top of the count, because the count alone
#: mis-prices the fast-fault mode: an OSError that returns immediately
#: completes a loop turn in ~0.07 s (queue poll 0.05 s + backoff <= 0.02 s),
#: so every 50th fault lands every ~3.5 s -- roughly 1000 error lines per
#: hour.  Count-triggered events closer than this to the previous one are
#: held back; the running count (consecutiveFaults) and the recovered line
#: still carry the exact total, and slow faults (each waiting out the busy
#: timeout before returning) never notice the floor: 50 of them take over
#: 100 s.
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


class LifecycleOperationConflict(RuntimeError):
    pass


class LifecycleStepFailed(RuntimeError):
    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.code = code


class LifecycleStepUnresolved(RuntimeError):
    """An admitted effect may have run but has no committed completion yet."""


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
        (tests/test_lifecycle_journal_retention_gate.py) only scans
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


class LifecycleProcessManager:
    """Bounded durable saga runner intended for the daemon composition root."""

    def __init__(
        self,
        state: Path | StateDatabase,
        ports: LifecyclePorts,
        router: CorrelationEventRouter,
        *,
        capacity: int = 32,
        completion_timeout: float = 2.0,
        admission_deadline: float = 1.0,
        operation_deadline: float = LIFECYCLE_OPERATION_DEADLINE_SECONDS,
        admission_backoff: tuple[float, ...] = (0.005, 0.01, 0.02),
        fault_after_effect: Callable[[str, str], None] | None = None,
        fault_after_receipt_retire: Callable[[str, str], None] | None = None,
        event_sink: Callable[..., None] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._store = _LifecycleStore(
            state if isinstance(state, StateDatabase) else StateDatabase(Path(state))
        )
        self._generation = self._store.next_generation()
        # U0c (Allen 2026-09-03): a saga that did not finish in its daemon
        # generation starts over after a restart -- it is never resumed.
        # Before the worker thread exists, every RUNNING journal row belongs
        # to a previous generation, so it is marked for compensation here;
        # ``recover`` then schedules the teardown, and desired-state re-runs
        # whatever is still wanted.  This must happen before ``_thread``
        # starts so the new generation can never observe a stale RUNNING
        # row as forward progress.
        self._restart_interrupted = self._store.interrupt_running_operations(
            "saga did not finish in its daemon generation; "
            "restart compensates instead of resuming",
            "SAGA_INTERRUPTED_BY_RESTART",
        )
        self._ports = ports
        self._router = router
        self._capacity = capacity
        self._completion_timeout = completion_timeout
        self._admission_deadline = admission_deadline
        self._operation_deadline = operation_deadline
        #: Monotonic start of each operations current re-drive life, used only
        #: to bound an unresolvable step against ``_operation_deadline``.
        self._operation_started_at: dict[str, float] = {}
        self._backoff = admission_backoff
        self._fault_after_effect = fault_after_effect
        self._fault_after_receipt_retire = fault_after_receipt_retire
        self._queue: Queue[str | None] = Queue(maxsize=capacity)
        self._condition = threading.Condition()
        self._scheduled: set[str] = set()
        self._active: str | None = None
        self._closed = False
        self._crashed = False
        self._store_closed = False
        #: Optional observability sink (the daemon's event log in production).
        #: Thread faults are state first (``_crashed``/``_last_error``) and
        #: events second, so a failing sink can never mask or cause a death.
        self._event_sink = event_sink
        self._last_error: str | None = None
        self._recover_faults = 0
        # Consumer-thread-only bookkeeping for the event throttle's clock
        # floor (see _RECOVER_FAULT_EVENT_MIN_INTERVAL_S); monotonic, reset
        # per manager generation together with the fault count.
        self._last_recover_event_at = 0.0
        self._thread = threading.Thread(
            target=self._run, name="hyprial-lifecycle-process-manager", daemon=True
        )
        self._thread.start()
        self.recover()

    @property
    def crashed(self) -> bool:
        """Whether the consumer thread died; ``submit`` refuses in this state."""

        with self._condition:
            return self._crashed

    @property
    def last_error(self) -> str | None:
        """The fault that killed (or most recently stung) the consumer thread."""

        with self._condition:
            return self._last_error

    @property
    def is_running(self) -> bool:
        """Whether the consumer thread is alive (drained or crashed ⇒ False)."""

        return self._thread.is_alive()

    @property
    def state_db(self) -> StateDatabase:
        """The StateDatabase this journal was wired with.

        Reachability seam for the assembly invariant: the daemon must wire
        the SAME instance into the journal and the desired-state store, so
        "one transaction across both" is a fact about constructed objects,
        not a convention (tests/test_final_composition_lifecycle.py walks
        the assembled application down to this attribute).
        """

        return self._store._state_db

    def submit(self, operation: LifecycleOperation) -> PortAdmission:
        if not operation.operation_id.strip():
            raise ValueError("operation_id must not be blank")
        with self._condition:
            if self._closed or self._crashed:
                return PortAdmission.CLOSING
            _created, state = self._store.reserve(operation)
            if state in {
                LifecycleState.COMPLETED,
                LifecycleState.COMPENSATED,
                LifecycleState.FAILED,
            }:
                return PortAdmission.ACCEPTED
            self._schedule(operation.operation_id)
        # Durable reserve is the custody transfer.  Queue saturation cannot
        # turn it back into an ephemeral rejection; recover/scanning retries.
        return PortAdmission.ACCEPTED

    def recover(self) -> None:
        for operation_id in self._store.pending():
            self._schedule(operation_id)

    def result(self, operation_id: str) -> LifecycleResult:
        return self._store.result(operation_id)

    def wait(self, operation_id: str, timeout: float) -> LifecycleResult:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            result = self.result(operation_id)
            if result.state not in {
                LifecycleState.RUNNING,
                LifecycleState.COMPENSATING,
            }:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"lifecycle operation still running: {operation_id}")
            with self._condition:
                if self._crashed:
                    # Nobody will drive this operation in the current manager
                    # generation; saying so now beats waiting out the full
                    # timeout for a settlement that cannot arrive.
                    raise TimeoutError(
                        "lifecycle manager thread is dead; "
                        f"{operation_id} will not settle in this generation"
                    )
                self._condition.wait(min(0.02, remaining))

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            while self._active is not None or self._store.pending():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._crashed:
                    return False
                self._condition.wait(min(0.05, remaining))
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(max(0.0, deadline - time.monotonic()))
        complete = not self._thread.is_alive()
        if complete and not self._store_closed:
            self._store.close()
            self._store_closed = True
        return complete

    def _schedule(self, operation_id: str) -> None:
        with self._condition:
            if operation_id in self._scheduled or self._active == operation_id:
                return
            try:
                self._queue.put_nowait(operation_id)
            except Full:
                return
            self._scheduled.add(operation_id)
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            try:
                operation_id = self._queue.get(timeout=0.05)
            except Empty:
                if not self._guarded_recover():
                    return
                if self._close_requested_and_drained():
                    return
                continue
            if operation_id is None:
                self._queue.task_done()
                return
            with self._condition:
                self._scheduled.discard(operation_id)
                self._active = operation_id
            self._operation_started_at.setdefault(operation_id, time.monotonic())
            try:
                self._execute(operation_id)
            except _InjectedManagerCrash as error:
                self._mark_crashed(
                    f"injected manager crash: {error.__cause__ or 'fault hook'}"
                )
                return
            except BaseException as error:
                # Preserve RUNNING/COMPENSATING journal state for a new
                # process-manager generation; unexpected faults are not
                # translated into a business rejection.  The exit is loud:
                # ``_mark_crashed`` records state, wakes waiters and emits
                # the thread_exited event, and ``submit`` refuses from here.
                self._mark_crashed(f"{type(error).__name__}: {error}")
                return
            finally:
                self._queue.task_done()
                try:
                    state = self._store.state(operation_id)
                except (OSError, sqlite3.OperationalError):
                    # An unreadable journal is not a terminal state; keep the
                    # re-drive marker so the operation deadline still bounds it.
                    state = None
                if state is not None and state not in {
                    LifecycleState.RUNNING,
                    LifecycleState.COMPENSATING,
                }:
                    self._operation_started_at.pop(operation_id, None)
                with self._condition:
                    self._active = None
                    self._condition.notify_all()
            if not self._guarded_recover():
                return

    def _guarded_recover(self) -> bool:
        """One recovery scan that cannot silently kill the consumer thread.

        Retryable store faults -- a locked database under write contention
        (the 2026-09-14 production thread death) or a transient I/O error --
        are journaled, backed off and retried on the next pass; the consumer
        thread only leaves via drain/close, so ``submit`` never returns
        ACCEPTED into a queue nobody drains.  Any other exception is a real
        crash: recorded, emitted, and exited loudly (``_crashed`` set).

        Returns False when the thread must exit.
        """

        try:
            self.recover()
        except (OSError, sqlite3.OperationalError) as error:
            self._note_recover_fault(error)
            return True
        except BaseException as error:
            self._mark_crashed(f"recover: {type(error).__name__}: {error}")
            return False
        if self._recover_faults:
            # Close the episode loudly too: a recovered line with the total
            # fault count, so a log read can tell a blip from a storm.
            self._emit("thread_recovered", consecutiveFaults=self._recover_faults)
        self._recover_faults = 0
        return True

    def _close_requested_and_drained(self) -> bool:
        """The Empty-branch exit check, with the same fault discipline."""

        with self._condition:
            if not self._closed:
                return False
        try:
            pending = bool(self._store.pending())
        except (OSError, sqlite3.OperationalError) as error:
            self._note_recover_fault(error)
            return False
        except BaseException as error:
            self._mark_crashed(f"close-drain check: {type(error).__name__}: {error}")
            return True
        return not pending

    def _note_recover_fault(self, error: BaseException) -> None:
        self._recover_faults += 1
        detail = f"{type(error).__name__}: {error}"
        with self._condition:
            self._last_error = detail
        now = time.monotonic()
        if (
            self._recover_faults == 1
            or (
                self._recover_faults % _RECOVER_FAULT_EVENT_EVERY == 0
                and now - self._last_recover_event_at
                >= _RECOVER_FAULT_EVENT_MIN_INTERVAL_S
            )
        ):
            self._last_recover_event_at = now
            self._emit(
                "thread_error",
                detail=detail,
                consecutiveFaults=self._recover_faults,
            )
        # Reuse the admission backoff cadence (bounded, already tuned); the
        # queue poll above keeps the loop responsive to real work meanwhile.
        time.sleep(self._backoff[min(self._recover_faults - 1, len(self._backoff) - 1)])

    def _mark_crashed(self, detail: str) -> None:
        with self._condition:
            self._crashed = True
            self._last_error = detail
            self._condition.notify_all()
        self._emit("thread_exited", detail=detail)

    def _emit(self, event: str, **fields: object) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(event, **fields)
        except Exception:
            # The sink failing must never take the consumer thread down; the
            # state half (_crashed / _last_error) is already recorded, so the
            # fault stays visible through ps even when the log write fails.
            pass

    def _execute(self, operation_id: str) -> None:
        operation = self._store.load(operation_id)
        steps = _plan(operation)
        if self._store.state(operation_id) is LifecycleState.COMPENSATING:
            prior = self._store.result(operation_id)
            self._compensate(
                operation_id,
                steps,
                prior.error or "recovered compensation",
                prior.error_code,
            )
            return
        try:
            for ordinal, step in enumerate(steps):
                if self._store.effect_done(operation_id, step.name, "forward"):
                    self._retire_completed_receipt(operation_id, step, "forward")
                    continue
                self._perform(operation_id, ordinal, step, "forward")
            self._store.set_state(operation_id, LifecycleState.COMPLETED)
        except LifecycleStepFailed as error:
            self._store.set_state(
                operation_id,
                LifecycleState.COMPENSATING,
                str(error),
                error.code,
            )
            self._compensate(operation_id, steps, str(error), error.code)
        except LifecycleStepUnresolved:
            # The domain or I/O worker still owns an admitted effect.  It is
            # unsafe to compensate or declare success until a fenced receipt
            # arrives; a later recovery scan resubmits/reassociates it -- unless
            # the operation has outlived its deadline, in which case the effect
            # is not "slow" but stuck (card 104164aa (c)).  Fail it terminally,
            # never COMPENSATING: compensating an unstoppable stop hangs the same
            # way in reverse.  recover() will not reschedule a FAILED operation,
            # so the re-drive stops and the caller gets a coded failure.
            started = self._operation_started_at.get(operation_id)
            if (
                started is not None
                and time.monotonic() - started >= self._operation_deadline
            ):
                detail = (
                    "lifecycle operation exceeded its "
                    f"{self._operation_deadline:g}s deadline with a step still unresolved"
                )
                code = "LIFECYCLE_OPERATION_TIMEOUT"
                if (step.domain == "harness" and step.forward == "remove"
                        and not self._store.effect_done(operation_id, step.name, "forward")):
                    receipt = self._store.effect_receipt(operation_id, step.name, "forward")
                    assert receipt is not None
                    request = LifecycleMutationRequest(
                        receipt.correlation_id, receipt.attempt_token, operation_id, None,
                        RemoveHarnessCommand(
                            receipt.correlation_id, step.spec.harness.harness, step.spec.harness.name,
                        ),
                    )
                    try:
                        settled = self._ports.harness.fail_lifecycle(
                            request, code=code, detail=detail,
                            timeout=LIFECYCLE_WAIT_MARGIN_SECONDS / 2,
                        )
                    except TimeoutError:
                        # No actor acknowledgement is NOT a domain settlement.
                        # Keep custody and retry, never mutate its rows here.
                        return
                    if isinstance(settled, LifecycleMutationCompleted):
                        self._store.complete_effect(
                            operation_id, step.name, "forward", receipt.correlation_id,
                            provenance=settled.provenance,
                        )
                        self._retire_completed_receipt(operation_id, step, "forward")
                        return
                    code, detail = settled.code, settled.detail
                self._store.set_state(
                    operation_id, LifecycleState.FAILED, detail, code,
                )
                self._operation_started_at.pop(operation_id, None)
                return
            time.sleep(min(0.02, self._completion_timeout))

    def _compensate(
        self,
        operation_id: str,
        steps: tuple[_Step, ...],
        cause: str,
        error_code: str | None,
    ) -> None:
        completed = set(self._store.compensable_forward(operation_id))
        try:
            for ordinal, step in reversed(tuple(enumerate(steps))):
                if step.name not in completed:
                    continue
                if self._store.effect_done(operation_id, step.name, "compensation"):
                    self._retire_completed_receipt(operation_id, step, "compensation")
                    continue
                self._perform(operation_id, ordinal, step, "compensation")
        except LifecycleStepFailed as error:
            self._store.set_state(
                operation_id,
                LifecycleState.FAILED,
                f"{cause}; compensation failed: {error}",
                error_code,
            )
            return
        except LifecycleStepUnresolved:
            time.sleep(min(0.02, self._completion_timeout))
            return
        self._store.set_state(
            operation_id, LifecycleState.COMPENSATED, cause, error_code
        )

    def _perform(
        self,
        operation_id: str,
        ordinal: int,
        step: _Step,
        direction: str,
    ) -> None:
        attempt_token = f"{operation_id}:{direction}:{ordinal}"
        correlation = f"lifecycle:{attempt_token}"
        port = getattr(self._ports, step.domain)
        generation = self._generation if step.domain == "route" else port.generation
        version = ordinal + 1 if step.domain == "route" else port.version
        command = _command(
            step,
            direction,
            correlation=correlation,
            attempt_token=attempt_token,
            generation=generation,
            version=version,
        )
        expected_resource_token = (
            None
            if direction == "forward"
            else self._store.forward_resource_token(operation_id, step.name)
        )
        receipt = self._store.effect_receipt(operation_id, step.name, direction)
        if receipt is None:
            receipt = self._store.prepare_effect(
                operation_id,
                ordinal,
                step.name,
                direction,
                correlation,
                attempt_token,
            )
        request = LifecycleMutationRequest(
            receipt.correlation_id,
            receipt.attempt_token,
            operation_id,
            expected_resource_token,
            command,
        )
        try:
            waiter = self._router.register(
                receipt.correlation_id,
                attempt_token=receipt.attempt_token,
                generation=generation,
                versions=(
                    frozenset({version})
                    if step.domain == "route"
                    else frozenset({version, version + 1})
                ),
            )
        except CorrelationRouterOverloaded as error:
            raise LifecycleStepFailed(str(error)) from error
        try:
            self._store.mark_dispatched(operation_id, step.name, direction)
            self._admit(step.domain, request)
            event = waiter.wait(self._completion_timeout)
        except TimeoutError as error:
            waiter.cancel()
            raise LifecycleStepUnresolved(
                f"{step.name} completion deadline elapsed"
            ) from error
        except BaseException:
            waiter.cancel()
            raise
        if (
            isinstance(event, RouteMutationFailed)
            and event.code == "ROUTE_PARTIAL_CLEANUP"
        ):
            raise LifecycleStepUnresolved(f"{step.name} route cleanup remains partial")
        if isinstance(event, LifecycleMutationFailed):
            raise LifecycleStepFailed(
                f"{step.name} failed: {event.code}: {event.detail}",
                code=event.code,
            )
        if isinstance(event, (PortCommandRejected, RouteMutationFailed)):
            raise LifecycleStepFailed(
                f"{step.name} rejected: {event.code}: {event.detail}",
                code=event.code,
            )
        provenance = _completion_provenance(step.domain, event)
        self._store.complete_effect(
            operation_id,
            step.name,
            direction,
            correlation,
            provenance=provenance,
        )
        if self._fault_after_effect is not None:
            try:
                self._fault_after_effect(operation_id, step.name)
            except Exception as error:
                raise _InjectedManagerCrash() from error
        self._retire_completed_receipt(operation_id, step, direction)

    def _retire_completed_receipt(
        self, operation_id: str, step: _Step, direction: str
    ) -> None:
        retirement = self._store.receipt_retirement(operation_id, step.name, direction)
        port = getattr(self._ports, step.domain)
        if retirement is not None:
            if not port.retire_receipt(
                retirement.attempt_token, retirement.resource_token
            ):
                raise LifecycleStepUnresolved(
                    f"{step.name} domain receipt is not yet retireable"
                )
            if self._fault_after_receipt_retire is not None:
                try:
                    self._fault_after_receipt_retire(operation_id, step.name)
                except Exception as error:
                    raise _InjectedManagerCrash() from error
            self._store.mark_receipt_retired(operation_id, step.name, direction)
        completed = self._store.completed_receipt(operation_id, step.name, direction)
        port.confirm_receipt_retired(completed.attempt_token, completed.resource_token)

    def _admit(self, domain: str, command: LifecycleMutationRequest) -> None:
        port = getattr(self._ports, domain)
        deadline = time.monotonic() + self._admission_deadline
        attempt = 0
        while True:
            admission = port.submit(command)
            if admission is PortAdmission.ACCEPTED:
                return
            if domain == "route" and admission is PortAdmission.OVERLOADED:
                route_command = command.payload
                if isinstance(route_command, (EnsureRouteCommand, DropRouteCommand)):
                    if self._ports.route.reassociate(
                        route_command.attempt_token,
                        correlation_id=route_command.correlation_id,
                        generation=route_command.generation,
                        version=route_command.version,
                    ):
                        return
            if admission is PortAdmission.CLOSING:
                raise LifecycleStepFailed(
                    f"{domain} command port is closing", code="PORT_CLOSING"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LifecycleStepFailed(
                    f"{domain} command port stayed overloaded",
                    code="PORT_OVERLOADED",
                )
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            time.sleep(min(delay, remaining))


def _create_steps(spec: LifecycleSpec, prefix: str = "") -> tuple[_Step, ...]:
    steps = [
        _Step(f"{prefix}agent.create", "agent", spec, "create", "destroy"),
        _Step(
            f"{prefix}route.persona.ensure",
            "route",
            spec,
            "ensure",
            "drop",
            f"persona:{spec.actor}",
        ),
        _Step(f"{prefix}agent.bind", "agent", spec, "bind", "release"),
        _Step(f"{prefix}harness.ensure", "harness", spec, "ensure", "remove"),
    ]
    if spec.session is not None:
        steps.append(
            _Step(
                f"{prefix}session.register", "session", spec, "register", "unregister"
            )
        )
    steps.append(
        _Step(
            f"{prefix}route.managed.ensure",
            "route",
            spec,
            "ensure",
            "drop",
            f"managed:{spec.actor}",
        )
    )
    return tuple(steps)


def _remove_steps(spec: LifecycleSpec, prefix: str = "") -> tuple[_Step, ...]:
    return tuple(
        _Step(
            f"{prefix}{step.name.split('.', 1)[1]}.{step.inverse}",
            step.domain,
            spec,
            step.inverse,
            step.forward,
            step.route_owner,
        )
        for step in reversed(_create_steps(spec))
    )


def _deactivate_steps(
    spec: LifecycleSpec, prefix: str = ""
) -> tuple[_Step, ...]:
    """Reverse runtime effects but retain the Agent identity and its pins."""

    steps = list(reversed(_create_steps(spec)))
    return tuple(
        _Step(
            f"{prefix}{step.name.split('.', 1)[1]}.{step.inverse}",
            step.domain,
            spec,
            step.inverse,
            step.forward,
            step.route_owner,
        )
        for step in steps
        if step.name
        not in {f"{prefix}agent.create", f"{prefix}route.persona.ensure"}
    )


def _plan(operation: LifecycleOperation) -> tuple[_Step, ...]:
    if operation.kind is LifecycleKind.CREATE:
        return _create_steps(operation.target)
    if operation.kind is LifecycleKind.DEACTIVATE:
        return _deactivate_steps(operation.target)
    if operation.kind is LifecycleKind.REMOVE:
        return _remove_steps(operation.target)
    if operation.source is None:
        raise ValueError("transfer requires a source spec")
    return (
        *_remove_steps(operation.source, "source."),
        *_create_steps(operation.target, "target."),
    )


def _command(
    step: _Step,
    direction: str,
    *,
    correlation: str,
    attempt_token: str,
    generation: int,
    version: int,
) -> object:
    operation = step.forward if direction == "forward" else step.inverse
    spec = step.spec
    if step.domain == "agent":
        if operation == "create":
            return CreateAgentCommand(correlation, spec.agent_name, reuse_existing=True)
        if operation == "destroy":
            return DestroyAgentCommand(correlation, spec.agent_name)
        if operation == "bind":
            return BindAgentCommand(
                correlation,
                spec.actor,
                spec.harness.harness,
                "headless" if spec.session is None else spec.session.runtime,
                (
                    spec.harness.session_ref
                    if spec.session is None
                    else spec.session.session_ref
                ),
            )
        return ReleaseAgentCommand(correlation, spec.actor)
    if step.domain == "harness":
        if operation == "ensure":
            return EnsureHarnessCommand(correlation, spec.harness)
        return RemoveHarnessCommand(
            correlation, spec.harness.harness, spec.harness.name
        )
    if step.domain == "session":
        session = spec.session
        if session is None:
            raise ValueError("session step requires session spec")
        if operation == "register":
            return RegisterSessionCommand(
                correlation,
                spec.actor,
                session.cwd,
                session.command,
                session.source,
                session.session_ref,
                runtime=session.runtime,
            )
        return UnregisterSessionCommand(correlation, spec.actor, session.session_ref)
    if operation == "ensure":
        return EnsureRouteCommand(
            correlation,
            attempt_token,
            generation,
            version,
            spec.route,
            step.route_owner,
        )
    return DropRouteCommand(
        correlation,
        attempt_token,
        generation,
        version,
        spec.route.route_id,
        step.route_owner,
    )


def _completion_provenance(domain: str, event: CorrelatedEvent) -> MutationProvenance:
    expected: dict[str, type[object]] = {
        "agent": AgentMutationCompleted,
        "session": SessionMutationCompleted,
        "harness": HarnessMutationCompleted,
    }
    if domain == "route":
        # Route failures were handled above; successful route events are the
        # remaining half of the closed union.
        if not hasattr(event, "attempt_token"):
            raise LifecycleStepFailed("route returned an invalid completion")
        provenance = getattr(event, "provenance", None)
        if not isinstance(provenance, MutationProvenance):
            raise LifecycleStepFailed("route completion lacks provenance receipt")
        if bool(getattr(event, "changed", False)) != provenance.changed:
            raise LifecycleStepFailed("route completion/provenance changed mismatch")
        return provenance
    if not isinstance(event, LifecycleMutationCompleted):
        raise LifecycleStepFailed(
            f"{domain} completion lacks atomic provenance receipt"
        )
    if event.domain != domain or not isinstance(event.payload, expected[domain]):
        raise LifecycleStepFailed(
            f"{domain} returned unexpected completion {type(event).__name__}"
        )
    payload_changed = getattr(event.payload, "changed", None)
    if (
        isinstance(payload_changed, bool)
        and payload_changed != event.provenance.changed
    ):
        raise LifecycleStepFailed(f"{domain} completion/provenance changed mismatch")
    return event.provenance


def _spec_payload(spec: LifecycleSpec) -> dict[str, object]:
    session = None if spec.session is None else asdict(spec.session)
    if session is not None:
        session["command"] = list(spec.session.command)
    harness = spec.harness.to_payload()
    return {
        "agentName": spec.agent_name,
        "actor": spec.actor,
        "harness": harness,
        "route": asdict(spec.route),
        "session": session,
    }


def _operation_json(operation: LifecycleOperation) -> str:
    return json.dumps(
        {
            "operationId": operation.operation_id,
            "kind": operation.kind.value,
            "target": _spec_payload(operation.target),
            "source": None
            if operation.source is None
            else _spec_payload(operation.source),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _operation_from_json(payload: str) -> LifecycleOperation:
    raw = json.loads(payload)
    return LifecycleOperation(
        str(raw["operationId"]),
        LifecycleKind(str(raw["kind"])),
        _spec_from_payload(raw["target"]),
        None if raw["source"] is None else _spec_from_payload(raw["source"]),
    )


def _spec_from_payload(raw: dict[str, object]) -> LifecycleSpec:
    harness = raw["harness"]
    route = raw["route"]
    session = raw["session"]
    if not isinstance(harness, dict) or not isinstance(route, dict):
        raise ValueError("invalid lifecycle journal payload")
    return LifecycleSpec(
        agent_name=str(raw["agentName"]),
        actor=str(raw["actor"]),
        harness=HarnessLaunchProjection(
            harness=str(harness["provider"]),
            name=str(harness["name"]),
            headless=bool(harness["headless"]),
            args=tuple(str(item) for item in harness.get("args", [])),
            ownership=str(harness.get("ownership", "managed")),
            nickname=_optional_str(harness.get("nickname")),
            cwd=_optional_str(harness.get("cwd")),
            endpoint=_optional_str(harness.get("endpoint")),
            session_ref=_optional_str(harness.get("sessionRef")),
            command=tuple(str(item) for item in harness.get("command", [])),
            turn_timeout_seconds=_optional_float(harness.get("turnTimeoutSeconds")),
            idle_timeout_seconds=_optional_float(harness.get("idleTimeoutSeconds")),
            containerized=bool(harness.get("containerized", False)),
            pinned_owner=_optional_str(harness.get("pinnedOwner")),
            container_image=_optional_str(harness.get("containerImage")),
            model_provider=_optional_str(harness.get("modelProvider")),
            model=_optional_str(harness.get("model")),
        ),
        route=RouteSpec(
            str(route["route_id"]),
            str(route["liveliness_key"]),
            str(route["inbox_key"]),
            bool(route.get("advertise", True)),
        ),
        session=(
            None
            if session is None
            else SessionLifecycleSpec(
                cwd=str(session["cwd"]),
                command=tuple(str(item) for item in session["command"]),
                source=str(session["source"]),
                session_ref=str(session["session_ref"]),
                runtime=str(session.get("runtime", "claude_interactive")),
            )
        ),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
