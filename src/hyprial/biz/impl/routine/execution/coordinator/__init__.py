"""Durable coordinator for routine registration and retirement sagas.

The routine registry keeps its existing writer actor.  This actor owns only
the cross-domain operation journal.  Lifecycle, PAC and routine calls run on
bounded effect workers; their completed steps return to this mailbox before
the next step is admitted.  Every step can be replayed after a crash using
the operation ID and deterministic routine registration ID.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import PortAdmission

from hyprial.biz.impl.routine.execution.service  import RoutineServiceError
from hyprial.biz.impl.routine.contracts.schema  import RoutineSchemaError, load_routine_text
from hyprial.biz.impl.routine.execution.coordinator.generation import (
    AgentCreationCompensation,
    BeginAdd,
    BeginRemove,
    RoutineCoordinatorError,
    RoutineCoordinatorTimeout,
    RoutineOperationProjection,
    RoutinePort,
    _Effect,
    _EffectCompleted,
    _Generation,
    _Recover,
    _SCHEMA,
)

__all__ = [
    "RoutineCoordinator",
    "RoutineCoordinatorError",
    "RoutineCoordinatorTimeout",
    "RoutineOperationProjection",
    "add_command",
    "remove_command",
]


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
