"""Durable coordinator for routine registration and retirement sagas.

The routine registry keeps its existing writer actor.  This actor owns only
the cross-domain operation journal.  Lifecycle, PAC and routine calls run on
bounded effect workers; their completed steps return to this mailbox before
the next step is admitted.  Every step can be replayed after a crash using
the operation ID and deterministic routine registration ID.
"""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.contracts.ports import PortAdmission

from .service import RoutineServiceError
from .schema import RoutineSchemaError, load_routine_text


class RoutinePort(Protocol):
    def add(
        self, *, yaml_text: str, owner: str, enabled: bool,
        registration_id: str | None = None,
    ) -> dict[str, object]: ...
    def status(self, *, name: str) -> dict[str, object]: ...
    def list(self) -> dict[str, object]: ...
    def pause(self, *, name: str) -> dict[str, object]: ...
    def resume(self, *, name: str, align_schedule: bool = False) -> dict[str, object]: ...
    def remove(
        self, *, name: str, enforce_last: bool = False,
        reservation_id: str | None = None,
    ) -> dict[str, object]: ...
    def reserve_remove(
        self, *, name: str, reservation_id: str, enforce_last: bool = False
    ) -> dict[str, object]: ...
    def cancel_remove(
        self, *, name: str, reservation_id: str
    ) -> dict[str, object]: ...
    def set(self, *, name: str, yaml_text: str) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class BeginAdd:
    correlation_id: str
    operation_id: str
    name: str
    yaml_text: str
    owner: str
    produces: str | None
    agent_compensation: AgentCreationCompensation | None = None


@dataclass(frozen=True, slots=True)
class AgentCreationCompensation:
    actor: str
    expected_entity_token: str
    settlement_id: str


@dataclass(frozen=True, slots=True)
class BeginRemove:
    correlation_id: str
    operation_id: str
    name: str
    enforce_last: bool = False


@dataclass(frozen=True, slots=True)
class RoutineOperationProjection:
    operation_id: str
    name: str
    kind: str
    state: str
    step: str
    result: dict[str, object] | None
    error_code: str | None
    error_detail: str | None
    error_data: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _Recover:
    generation: int


@dataclass(frozen=True, slots=True)
class _Effect:
    generation: int
    operation_id: str
    kind: str
    step: str
    name: str
    payload: dict[str, object]
    receipts: dict[str, object]


@dataclass(frozen=True, slots=True)
class _EffectCompleted:
    generation: int
    operation_id: str
    step: str
    result: dict[str, object] | None = None
    error_code: str | None = None
    error_detail: str | None = None
    error_data: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class _Operation:
    operation_id: str
    kind: str
    name: str
    request_digest: str
    payload: dict[str, object]
    state: str
    step: str
    receipts: dict[str, object] = field(default_factory=dict)
    result: dict[str, object] | None = None
    error_code: str | None = None
    error_detail: str | None = None
    version: int = -1

    def projection(self) -> RoutineOperationProjection:
        rejection = self.receipts.get("rejectionData")
        data: tuple[tuple[str, str], ...] = ()
        if (
            isinstance(rejection, dict)
            and rejection.get("code") == self.error_code
            and isinstance(rejection.get("data"), list)
        ):
            pairs: list[tuple[str, str]] = []
            for item in rejection["data"]:
                if (
                    isinstance(item, list)
                    and len(item) == 2
                    and all(isinstance(value, str) for value in item)
                ):
                    pairs.append((item[0], item[1]))
            data = tuple(pairs)
        return RoutineOperationProjection(
            self.operation_id, self.name, self.kind, self.state, self.step,
            deepcopy(self.result), self.error_code, self.error_detail,
            data,
        )


class RoutineCoordinatorError(RuntimeError):
    def __init__(
        self, code: str, detail: str, operation_id: str,
        data: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.operation_id = operation_id
        self.data = data


class RoutineCoordinatorTimeout(TimeoutError):
    def __init__(self, operation_id: str) -> None:
        super().__init__(f"routine operation remains unsettled: {operation_id}")
        self.operation_id = operation_id


_SCHEMA = """
CREATE TABLE IF NOT EXISTS routine_coordinator_operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    step TEXT NOT NULL,
    receipts_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error_detail TEXT,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS routine_coordinator_pending
ON routine_coordinator_operations(state,name);
"""


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_digest(kind: str, name: str, payload: dict[str, object]) -> str:
    return hashlib.sha256(
        _encoded({"kind": kind, "name": name, "payload": payload}).encode()
    ).hexdigest()


def _agent_settlement_id(entity_token: str) -> str:
    return f"agent-default-compensation:{entity_token}"


class _Generation:
    def __init__(self, owner: RoutineCoordinator, generation: int) -> None:
        self.owner = owner
        self.generation = generation
        self.operations = self._load_all()
        self.active_effects: set[str] = set()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.owner.database)
        db.execute("PRAGMA busy_timeout=2000")
        # synchronous is connection-local even though WAL mode persists.
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _load_all(self) -> dict[str, _Operation]:
        db = self._connect()
        result: dict[str, _Operation] = {}
        try:
            rows = db.execute("SELECT * FROM routine_coordinator_operations").fetchall()
            for row in rows:
                op = _Operation(
                    operation_id=str(row[0]), kind=str(row[1]), name=str(row[2]),
                    request_digest=str(row[3]), payload=json.loads(row[4]),
                    state=str(row[5]), step=str(row[6]),
                    receipts=json.loads(row[7]),
                    result=None if row[8] is None else json.loads(row[8]),
                    error_code=row[9], error_detail=row[10],
                    version=int(row[11]),
                )
                if op.kind == "remove" and "reservationId" not in op.payload:
                    legacy_step = op.step
                    op.payload = {
                        "reservationId": op.operation_id,
                        **(
                            {"enforceLast": True}
                            if bool(op.payload.get("enforceLast"))
                            else {}
                        ),
                    }
                    op.request_digest = _request_digest(
                        op.kind, op.name, op.payload
                    )
                    if op.state == "running":
                        if legacy_step != "snapshot":
                            op.receipts["legacyRemovalStarted"] = True
                            routine = op.receipts.get("routine")
                            op.receipts["removalWasEnabled"] = bool(
                                isinstance(routine, dict)
                                and routine.get("enabled") is True
                            )
                        op.step = "reserve"
                    cursor = db.execute(
                        "UPDATE routine_coordinator_operations SET "
                        "request_digest=?,payload_json=?,step=?,receipts_json=?,"
                        "version=version+1 "
                        "WHERE operation_id=? AND version=?",
                        (
                            op.request_digest, _encoded(op.payload), op.step,
                            _encoded(op.receipts), op.operation_id, op.version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoutineCoordinatorError(
                            "ROUTINE_OPERATION_CONFLICT",
                            "legacy remove journal changed during migration",
                            op.operation_id,
                        )
                    op.version += 1
                compensation = op.payload.get("agentCompensation")
                if (
                    op.kind == "add"
                    and isinstance(compensation, dict)
                    and "settlementId" not in compensation
                ):
                    updated = dict(compensation)
                    updated["settlementId"] = _agent_settlement_id(
                        str(updated["expectedEntityToken"])
                    )
                    op.payload = {**op.payload, "agentCompensation": updated}
                    op.request_digest = _request_digest(
                        op.kind, op.name, op.payload
                    )
                    cursor = db.execute(
                        "UPDATE routine_coordinator_operations SET "
                        "request_digest=?,payload_json=?,version=version+1 "
                        "WHERE operation_id=? AND version=?",
                        (
                            op.request_digest,
                            _encoded(op.payload),
                            op.operation_id,
                            op.version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoutineCoordinatorError(
                            "ROUTINE_OPERATION_CONFLICT",
                            "legacy Agent compensation changed during migration",
                            op.operation_id,
                        )
                    op.version += 1
                result[op.operation_id] = op
            db.commit()
        finally:
            db.close()
        for op in result.values():
            self.owner._publish(op.projection())
        return result

    def _persist(self, op: _Operation) -> None:
        db = self._connect()
        try:
            with db:
                if op.version < 0:
                    db.execute(
                        "INSERT INTO routine_coordinator_operations VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,0)",
                        (
                            op.operation_id, op.kind, op.name, op.request_digest,
                            _encoded(op.payload), op.state, op.step,
                            _encoded(op.receipts),
                            None if op.result is None else _encoded(op.result),
                            op.error_code, op.error_detail,
                        ),
                    )
                    op.version = 0
                else:
                    cursor = db.execute(
                        "UPDATE routine_coordinator_operations SET state=?,step=?,"
                        "receipts_json=?,result_json=?,error_code=?,error_detail=?,"
                        "version=version+1 WHERE operation_id=? AND request_digest=? "
                        "AND version=?",
                        (
                            op.state, op.step, _encoded(op.receipts),
                            None if op.result is None else _encoded(op.result),
                            op.error_code, op.error_detail, op.operation_id,
                            op.request_digest, op.version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RoutineCoordinatorError(
                            "ROUTINE_OPERATION_CONFLICT",
                            "routine coordinator journal changed in another generation",
                            op.operation_id,
                        )
                    op.version += 1
        finally:
            db.close()
        self.owner._publish(op.projection())

    def __call__(self, command: object) -> None:
        if isinstance(command, (BeginAdd, BeginRemove)):
            self._begin(command)
        elif isinstance(command, _EffectCompleted):
            self._completed(command)
        elif isinstance(command, _Recover) and command.generation == self.generation:
            for op in self.operations.values():
                if op.state == "running":
                    self._schedule(op)

    def _begin(self, command: BeginAdd | BeginRemove) -> None:
        payload: dict[str, object] = (
            {
                "yaml": command.yaml_text,
                "owner": command.owner,
                "produces": command.produces,
                "registrationId": hashlib.sha256(
                    command.operation_id.encode()
                ).hexdigest()[:32],
                **(
                    {
                        "agentCompensation": {
                            "actor": command.agent_compensation.actor,
                            "expectedEntityToken": (
                                command.agent_compensation.expected_entity_token
                            ),
                            "settlementId": command.agent_compensation.settlement_id,
                        }
                    }
                    if command.agent_compensation is not None
                    else {}
                ),
            }
            if isinstance(command, BeginAdd)
            else {
                "reservationId": command.operation_id,
                **({"enforceLast": True} if command.enforce_last else {}),
            }
        )
        kind = "add" if isinstance(command, BeginAdd) else "remove"
        digest = _request_digest(kind, command.name, payload)
        try:
            if not command.operation_id.strip() or not command.name.strip():
                raise RoutineCoordinatorError(
                    "ROUTINE_COORDINATOR_INVALID", "operation ID and routine name required",
                    command.operation_id,
                )
            existing = self.operations.get(command.operation_id)
            if existing is not None:
                if existing.request_digest != digest:
                    raise RoutineCoordinatorError(
                        "ROUTINE_OPERATION_CONFLICT", "operation ID reused with different request",
                        command.operation_id,
                    )
                self._schedule(existing)
                self.owner._ack(command.correlation_id, None)
                return
            if any(
                op.name == command.name and op.state == "running"
                for op in self.operations.values()
            ):
                raise RoutineCoordinatorError(
                    "ROUTINE_COORDINATOR_BUSY", f"routine {command.name} has an active operation",
                    command.operation_id,
                )
            op = _Operation(
                command.operation_id, kind, command.name, digest, payload,
                "running", "reserve",
            )
            self._persist(op)  # durable admission before ACK
            self.operations[op.operation_id] = op
            self.owner._ack(command.correlation_id, None)
            self._schedule(op)
        except Exception as error:
            self.owner._ack(command.correlation_id, error)

    def _schedule(self, op: _Operation) -> None:
        if op.state != "running" or op.operation_id in self.active_effects:
            return
        retry_after_ms = op.receipts.get("retryAfterMs")
        if (
            isinstance(retry_after_ms, int)
            and retry_after_ms > self.owner._clock_ms()
        ):
            return
        if not self.owner._reserve_effect(op.operation_id):
            return
        effect = _Effect(
            self.generation, op.operation_id, op.kind, op.step, op.name,
            deepcopy(op.payload), deepcopy(op.receipts),
        )
        try:
            self.owner._effects.put_nowait(effect)
        except queue.Full:
            self.owner._release_effect(op.operation_id)
            self.owner._note_effect_overload()
            return
        self.active_effects.add(op.operation_id)

    def _completed(self, completion: _EffectCompleted) -> None:
        if completion.generation != self.generation:
            return
        op = self.operations.get(completion.operation_id)
        self.active_effects.discard(completion.operation_id)
        if op is None or op.state != "running" or op.step != completion.step:
            return
        if completion.error_code is not None:
            if completion.error_data:
                op.receipts["rejectionData"] = {
                    "code": completion.error_code,
                    "data": [list(item) for item in completion.error_data],
                }
            if op.kind == "add" and op.step in {"ensure", "resume"}:
                op.receipts["originalError"] = {
                    "code": completion.error_code,
                    "detail": completion.error_detail or "",
                }
                op.step = "compensate"
            elif op.kind == "add" and op.step == "compensate":
                original = op.receipts.get("originalError", {})
                op.receipts["routineCompensationError"] = {
                    "code": completion.error_code,
                    "detail": completion.error_detail or "",
                }
                if "agentCompensation" in op.payload:
                    op.step = "agent_compensate"
                else:
                    op.state = "failed"
                    op.error_code = str(
                        original.get("code", "ROUTINE_COORDINATOR_FAILED")
                    )
                    op.error_detail = (
                        f"{original.get('detail', 'coordinator setup failed')}; "
                        f"compensation failed: {completion.error_code}: "
                        f"{completion.error_detail or ''}"
                    )
            elif op.kind == "add" and op.step == "agent_compensate":
                self._retain_retry(op, completion)
            elif op.kind == "add":
                op.receipts["originalError"] = {
                    "code": completion.error_code,
                    "detail": completion.error_detail or "",
                }
                if "agentCompensation" in op.payload:
                    op.step = "agent_compensate"
                else:
                    op.state = "failed"
                    op.error_code = completion.error_code
                    op.error_detail = completion.error_detail
            elif op.kind == "remove":
                if (
                    op.step == "reserve"
                    and op.receipts.get("legacyRemovalStarted") is True
                ):
                    op.receipts["originalError"] = {
                        "code": completion.error_code,
                        "detail": completion.error_detail or "",
                    }
                    op.step = "cancel_probe"
                elif op.step in {
                    "cancel_probe", "restore_coordinator", "restore_resume", "cancel"
                }:
                    self._retain_retry(op, completion)
                elif op.step != "reserve":
                    op.receipts["originalError"] = {
                        "code": completion.error_code,
                        "detail": completion.error_detail or "",
                    }
                    op.step = "cancel_probe"
                else:
                    op.state = "failed"
                    op.error_code = completion.error_code
                    op.error_detail = completion.error_detail
            else:
                op.state = "failed"
                op.error_code = completion.error_code
                op.error_detail = completion.error_detail
        elif op.kind == "add":
            if op.step == "reserve":
                op.receipts.update(completion.result or {})
                op.step = "ensure"
            elif op.step == "ensure":
                op.receipts["coordinator"] = (completion.result or {}).get("coordinator")
                op.step = "resume"
            elif op.step == "resume":
                result = dict(op.receipts.get("add", {}))
                result["enabled"] = True
                coordinator = op.receipts.get("coordinator")
                if coordinator is not None:
                    result["coordinator"] = coordinator
                op.result = result
                op.state = "completed"
            elif op.step == "compensate":
                original = op.receipts.get("originalError", {})
                if "agentCompensation" in op.payload:
                    op.step = "agent_compensate"
                else:
                    op.state = "failed"
                    op.error_code = str(
                        original.get("code", "ROUTINE_COORDINATOR_FAILED")
                    )
                    op.error_detail = str(
                        original.get("detail", "coordinator setup failed")
                    )
            elif op.step == "agent_compensate":
                original = op.receipts.get("originalError", {})
                op.receipts.pop("retryAfterMs", None)
                op.receipts.pop("retryLastError", None)
                op.receipts["agentCompensated"] = bool(
                    (completion.result or {}).get("destroyed")
                )
                op.state = "failed"
                op.error_code = str(
                    original.get("code", "ROUTINE_COORDINATOR_FAILED")
                )
                op.error_detail = str(
                    original.get("detail", "coordinator setup failed")
                )
        else:
            if op.step == "reserve":
                routine = (completion.result or {}).get("routine")
                if "removalWasEnabled" not in op.receipts:
                    op.receipts["removalWasEnabled"] = bool(
                        isinstance(routine, dict)
                        and routine.get("enabled") is True
                    )
                op.receipts["routine"] = routine
                if op.receipts["routine"] is None:
                    op.state = "completed"
                    op.result = {"removed": False}
                else:
                    op.step = "pause"
            elif op.step == "pause":
                op.step = "close_graphs"
            elif op.step == "close_graphs":
                op.step = "retire"
            elif op.step == "retire":
                op.receipts["coordinator"] = (completion.result or {}).get("coordinator")
                op.step = "delete"
            elif op.step == "delete":
                result = dict((completion.result or {}).get("result", {}))
                coordinator = op.receipts.get("coordinator")
                if coordinator is not None:
                    result["coordinator"] = coordinator
                op.result = result
                op.state = "completed"
            elif op.step == "cancel_probe":
                replacement = (completion.result or {}).get("routine")
                if replacement is None:
                    op.result = {"removed": True}
                    op.state = "completed"
                elif op.receipts.get("removalWasEnabled") is True:
                    op.step = "restore_coordinator"
                else:
                    op.step = "cancel"
            elif op.step == "restore_coordinator":
                op.step = "restore_resume"
            elif op.step == "restore_resume":
                op.step = "cancel"
            elif op.step == "cancel":
                original = op.receipts.get("originalError", {})
                op.state = "failed"
                op.error_code = str(
                    original.get("code", "ROUTINE_COORDINATOR_FAILED")
                )
                op.error_detail = str(
                    original.get("detail", "routine removal failed")
                )
        self._persist(op)
        if op.state == "running":
            self._schedule(op)

    def _retain_retry(
        self, op: _Operation, completion: _EffectCompleted
    ) -> None:
        attempts = int(op.receipts.get("retryAttempts", 0)) + 1
        op.receipts["retryAttempts"] = attempts
        op.receipts["retryLastError"] = {
            "code": completion.error_code or "ROUTINE_COORDINATOR_FAILED",
            "detail": completion.error_detail or "",
        }
        op.receipts["retryAfterMs"] = self.owner._clock_ms() + min(
            60_000, 500 * (2 ** min(attempts - 1, 7))
        )


class RoutineCoordinator:
    """Typed, durable routine add/remove saga.

    Call `begin` with an operation ID; ACCEPTED means the intent is journaled.
    `wait` reports the terminal result.  A timeout leaves accepted work in
    custody and recovery replays its current idempotent step.
    """

    def __init__(
        self, *, state_dir: Path, routines: RoutinePort,
        ensure_coordinator: Callable[[dict[str, object]], dict[str, object] | None],
        retire_coordinator: Callable[[dict[str, object]], dict[str, object] | None],
        close_graph: Callable[[str, str], object],
        compensate_agent: Callable[[str, str, str], bool] | None = None,
        clock_ms: Callable[[], int] | None = None,
        effect_capacity: int = 32, effect_workers: int = 4,
    ) -> None:
        self.database = Path(state_dir) / "routine-coordinator.sqlite3"
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.database)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(_SCHEMA)
        finally:
            db.close()
        self.routines = routines
        self.ensure_coordinator = ensure_coordinator
        self.retire_coordinator = retire_coordinator
        self.close_graph = close_graph
        self.compensate_agent = compensate_agent
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._condition = threading.Condition()
        self._projections: dict[str, RoutineOperationProjection] = {}
        self._acks: dict[str, tuple[threading.Event, list[Exception]]] = {}
        self._effect_custody: set[str] = set()
        self._effect_overloads = 0
        self._effects: queue.Queue[_Effect] = queue.Queue(maxsize=effect_capacity)
        self._closed = False
        self._generation = 0
        self._runtime = ActorRuntime()

        def factory() -> _Generation:
            self._generation += 1
            return _Generation(self, self._generation)

        self._handle = self._runtime.start(
            ActorSpec(
                name="routine-coordinator",
                handler_factory=factory,
                mailbox_capacity=128,
                supervision_profile="state_authority",
            )
        )
        self._workers = tuple(
            threading.Thread(
                target=self._effect_loop,
                name=f"routine-coordinator-effect-{index}",
                daemon=True,
            )
            for index in range(effect_workers)
        )
        for worker in self._workers:
            worker.start()
        self._recovery = threading.Thread(
            target=self._recover_loop, name="routine-coordinator-recover",
            daemon=True,
        )
        self._recovery.start()
        self._runtime.tell(self._handle, _Recover(self._generation))

    def begin(self, command: BeginAdd | BeginRemove, timeout: float = 5.0) -> PortAdmission:
        if isinstance(command, BeginAdd):
            try:
                spec = load_routine_text(command.yaml_text)
            except RoutineSchemaError as error:
                raise RoutineCoordinatorError(
                    "ROUTINE_SCHEMA_ERROR", str(error), command.operation_id
                ) from error
            if spec.name != command.name or spec.produces != command.produces:
                raise RoutineCoordinatorError(
                    "ROUTINE_COORDINATOR_INVALID",
                    "routine name or produced actor changed after validation",
                    command.operation_id,
                )
        acknowledgement = threading.Event()
        errors: list[Exception] = []
        with self._condition:
            if self._closed:
                return PortAdmission.CLOSING
            self._acks[command.correlation_id] = (acknowledgement, errors)
        admission = self._runtime.tell(self._handle, command)
        if admission is not AdmissionResult.ACCEPTED:
            with self._condition:
                self._acks.pop(command.correlation_id, None)
            return (
                PortAdmission.OVERLOADED
                if admission is AdmissionResult.OVERLOADED
                else PortAdmission.CLOSING
            )
        if not acknowledgement.wait(timeout):
            with self._condition:
                self._acks.pop(command.correlation_id, None)
            raise RoutineCoordinatorTimeout(command.operation_id)
        if errors:
            raise errors[0]
        return PortAdmission.ACCEPTED

    def wait(self, operation_id: str, timeout: float = 70.0) -> dict[str, object]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                projection = self._projections.get(operation_id)
                if projection is not None and projection.state == "completed":
                    return deepcopy(projection.result or {})
                if projection is not None and projection.state == "failed":
                    raise RoutineCoordinatorError(
                        projection.error_code or "ROUTINE_COORDINATOR_FAILED",
                        projection.error_detail or "routine operation failed",
                        operation_id,
                        projection.error_data,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RoutineCoordinatorTimeout(operation_id)
                self._condition.wait(remaining)

    def projection(self, operation_id: str) -> RoutineOperationProjection | None:
        with self._condition:
            projection = self._projections.get(operation_id)
            return None if projection is None else deepcopy(projection)

    def _publish(self, projection: RoutineOperationProjection) -> None:
        with self._condition:
            self._projections[projection.operation_id] = projection
            self._condition.notify_all()

    def _ack(self, correlation_id: str, error: Exception | None) -> None:
        with self._condition:
            pending = self._acks.pop(correlation_id, None)
            if pending is None:
                return
            event, errors = pending
            if error is not None:
                errors.append(error)
            event.set()

    def _reserve_effect(self, operation_id: str) -> bool:
        with self._condition:
            if self._closed or operation_id in self._effect_custody:
                return False
            self._effect_custody.add(operation_id)
            return True

    def _release_effect(self, operation_id: str) -> None:
        with self._condition:
            self._effect_custody.discard(operation_id)

    def _note_effect_overload(self) -> None:
        with self._condition:
            self._effect_overloads += 1

    def stats(self) -> dict[str, int]:
        """Observable bounded effect admission and durable pending custody."""

        with self._condition:
            return {
                "effectQueued": self._effects.qsize(),
                "effectCapacity": self._effects.maxsize,
                "effectInFlight": len(self._effect_custody),
                "effectOverloads": self._effect_overloads,
                "operationsPending": sum(
                    projection.state == "running"
                    for projection in self._projections.values()
                ),
            }

    def _effect_loop(self) -> None:
        while not self._closed:
            try:
                effect = self._effects.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                result = self._execute(effect)
                completion = _EffectCompleted(
                    effect.generation, effect.operation_id, effect.step, result,
                )
            except Exception as error:
                data = getattr(error, "data", ())
                completion = _EffectCompleted(
                    effect.generation, effect.operation_id, effect.step,
                    error_code=str(getattr(error, "code", type(error).__name__)),
                    error_detail=str(error)[:1000],
                    error_data=(
                        data
                        if isinstance(data, tuple)
                        and all(
                            isinstance(item, tuple)
                            and len(item) == 2
                            and all(isinstance(value, str) for value in item)
                            for item in data
                        )
                        else ()
                    ),
                )
            while not self._closed:
                admission = self._runtime.tell(self._handle, completion)
                if admission is AdmissionResult.ACCEPTED:
                    break
                if admission is AdmissionResult.CLOSED:
                    break
                time.sleep(0.01)
            self._release_effect(effect.operation_id)

    def _execute(self, effect: _Effect) -> dict[str, object]:
        name = effect.name
        if effect.kind == "add":
            if effect.step == "reserve":
                produces = effect.payload.get("produces")
                if isinstance(produces, str) and any(
                    item.get("produces") == produces and item.get("name") != name
                    for item in self.routines.list()["routines"]
                ):
                    raise RoutineCoordinatorError(
                        "ROUTINE_COORDINATOR_CONFLICT",
                        f"coordinator already owned: {produces}",
                        effect.operation_id,
                    )
                try:
                    added = self.routines.add(
                        yaml_text=str(effect.payload["yaml"]),
                        owner=str(effect.payload["owner"]), enabled=False,
                        registration_id=str(effect.payload["registrationId"]),
                    )
                except RoutineServiceError as error:
                    if error.code != "ROUTINE_EXISTS":
                        raise
                    existing = self.routines.status(name=name)
                    if (
                        existing.get("registrationId") != effect.payload["registrationId"]
                        or existing.get("owner") != effect.payload["owner"]
                        or existing.get("produces") != effect.payload["produces"]
                    ):
                        raise
                    added = existing
                return {"add": added, "routine": self.routines.status(name=name)}
            if effect.step == "ensure":
                routine = effect.receipts["routine"]
                assert isinstance(routine, dict)
                return {"coordinator": self.ensure_coordinator(routine)}
            if effect.step == "resume":
                self.routines.resume(name=name, align_schedule=True)
                return {}
            if effect.step == "compensate":
                routine = effect.receipts.get("routine")
                # Ensure may have started the coordinator and then lost its
                # completion.  The callback checks the durable ownership
                # marker before stopping anything, so run it even without a
                # recorded Ensure result.
                if isinstance(routine, dict):
                    self.retire_coordinator(routine)
                try:
                    self.routines.remove(name=name)
                except RoutineServiceError as error:
                    if error.code != "ROUTINE_NOT_FOUND":
                        raise
                return {}
            if effect.step == "agent_compensate":
                compensation = effect.payload.get("agentCompensation")
                if not isinstance(compensation, dict) or self.compensate_agent is None:
                    raise RoutineCoordinatorError(
                        "ROUTINE_COORDINATOR_INVALID",
                        "agent compensation callback or metadata is unavailable",
                        effect.operation_id,
                    )
                return {
                    "destroyed": self.compensate_agent(
                        str(compensation["settlementId"]),
                        str(compensation["actor"]),
                        str(compensation["expectedEntityToken"]),
                    )
                }
        elif effect.kind == "remove":
            reservation_id = str(effect.payload["reservationId"])
            if effect.step == "reserve":
                try:
                    routine = self.routines.reserve_remove(
                        name=name,
                        reservation_id=reservation_id,
                        enforce_last=bool(effect.payload.get("enforceLast")),
                    )
                except RoutineServiceError as error:
                    if error.code == "ROUTINE_NOT_FOUND":
                        return {"routine": None}
                    raise
                return {"routine": {**routine, "name": name}}
            if effect.step == "cancel":
                return self.routines.cancel_remove(
                    name=name, reservation_id=reservation_id
                )
            if effect.step == "cancel_probe":
                try:
                    return {"routine": self.routines.status(name=name)}
                except RoutineServiceError as error:
                    if error.code == "ROUTINE_NOT_FOUND":
                        return {"routine": None}
                    raise
            routine = effect.receipts.get("routine")
            assert isinstance(routine, dict)
            if effect.step == "restore_coordinator":
                return {"coordinator": self.ensure_coordinator(routine)}
            if effect.step == "restore_resume":
                self.routines.resume(name=name, align_schedule=True)
                return {}
            if effect.step == "pause":
                if routine.get("enabled") is True:
                    self.routines.pause(name=name)
                return {}
            if effect.step == "close_graphs":
                for item in routine.get("inFlight", []):
                    if isinstance(item, dict) and isinstance(item.get("runId"), str):
                        try:
                            self.close_graph(str(item["runId"]), str(routine["owner"]))
                        except Exception as error:
                            if getattr(error, "code", None) != "PAC_GRAPH_NOT_FOUND":
                                raise
                return {}
            if effect.step == "retire":
                return {"coordinator": self.retire_coordinator(routine)}
            if effect.step == "delete":
                try:
                    return {
                        "result": self.routines.remove(
                            name=name,
                            reservation_id=reservation_id,
                        )
                    }
                except RoutineServiceError as error:
                    if error.code == "ROUTINE_NOT_FOUND":
                        return {"result": {"removed": False}}
                    raise
        raise RuntimeError(f"unsupported routine coordinator step: {effect.kind}/{effect.step}")

    def _recover_loop(self) -> None:
        while not self._closed:
            time.sleep(0.2)
            if self._closed:
                return
            try:
                generation = self._runtime.snapshot(self._handle).generation
                self._runtime.tell(self._handle, _Recover(generation))
            except Exception:
                continue

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._recovery.join(max(0.0, deadline - time.monotonic()))
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        drained = self._runtime.drain(max(0.0, deadline - time.monotonic()))
        return (
            not self._recovery.is_alive()
            and not any(worker.is_alive() for worker in self._workers)
            and drained.complete
        )


def add_command(
    *, operation_id: str, name: str, yaml_text: str, owner: str,
    produces: str | None,
    agent_compensation: AgentCreationCompensation | None = None,
) -> BeginAdd:
    return BeginAdd(
        f"routine-add-{uuid4().hex}", operation_id, name, yaml_text, owner,
        produces, agent_compensation,
    )


def remove_command(
    *, operation_id: str, name: str, enforce_last: bool = False
) -> BeginRemove:
    return BeginRemove(
        f"routine-remove-{uuid4().hex}",
        operation_id,
        name,
        enforce_last,
    )
