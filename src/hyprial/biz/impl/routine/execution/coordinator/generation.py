"""Routine coordinator operation records and the generation effect runner."""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # forward reference to the owner class, no runtime import cycle
    from hyprial.biz.impl.routine.execution.coordinator import RoutineCoordinator


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
