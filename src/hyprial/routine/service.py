"""Routine IPC facade over the actor-owned registry.

The facade may wait at the daemon boundary and execute external effects. It
never mutates routine state; Store writes are serialized by ``RoutineRegistry``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Protocol, TypeVar
from uuid import uuid4

import yaml

from hyprial.actor_runtime import ActorRuntime
from hyprial.actor_runtime.contracts import ActorSpec, AdmissionResult
from hyprial.alarm import AlarmResult
from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.log import Logger

from .ports import (
    AddRoutineCommand,
    AddressMigrationProjection,
    PauseRoutineCommand,
    RecoverRoutinesCommand,
    RemoveRoutineCommand,
    ResumeRoutineCommand,
    RoutineInFlightProjection,
    RoutineMutationCompleted,
    RoutineProjection,
    RoutinesRecovered,
    RoutineSourceQueryCompleted,
    RoutineSourceTaskProjection,
    RoutineTimerElapsedCommand,
    RoutinePacIoCompleted,
)
from .registry import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    RegistryOutput,
    RoutineAlarmEffect,
    RoutineEffect,
    RoutineRegistry,
    RoutineSourceQueryEffect,
    RoutinePacEffect,
)
from .schema import RoutineSchemaError, load_routine_text
from .source import (
    FilePacJournal,
    PacJournalV2,
    SourcePermanentError,
    SourceTransientError,
    SourceTask,
    query_pac_journal,
    query_taskwarrior,
)
from .store import RoutineSnapshot, RoutineStore


class RoutineServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AlarmSink(Protocol):
    def escalate(
        self,
        *,
        to: str,
        text: str,
        reason: str | None = None,
        conversation_id: str = "workflow",
    ) -> "AlarmResult": ...


class PacPort(Protocol):
    """PAC dispatch seam (U3).  ``hyprial.routine.pac_dispatch`` implements it.

    ``start_idempotent`` is keyed by routine + task uuid, so a replay returns
    the same graph instead of dispatching twice; ``status`` projects the task's
    graph; ``close`` retires a settled graph.
    """

    def start_idempotent(
        self,
        *,
        routine_name: str,
        task_uuid: str,
        task_text: str,
        target: str,
        escalate_to: str,
        timeout_seconds: float,
        sender: str,
    ) -> dict[str, object]: ...

    def status(self, *, graph_id: str) -> dict[str, object]: ...

    def close(self, *, graph_id: str, actor: str) -> None: ...


SourceQuery = Callable[[str], list[SourceTask]]
_ResultT = TypeVar("_ResultT")
_STOP = object()


def _wall_ms() -> int:
    import time as time_module

    return time_module.time_ns() // 1_000_000


class RoutineFacade:
    """Backward-compatible routine.* facade; Registry owns every mutation."""

    def __init__(
        self,
        *,
        pac: PacPort,
        alarm: AlarmSink,
        state_dir: Path,
        clock_ms: Callable[[], int] | None = None,
        source_query: SourceQuery | None = None,
        pac_journal: PacJournalV2 | None = None,
        mailbox_capacity: int = 128,
        event_sink: Callable[[object], None] | None = None,
        migrate_address: Callable[[str], str | None] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._pac = pac
        self._alarm = alarm
        self._migrate_address = migrate_address
        self._logger = logger
        self._clock_ms = clock_ms or _wall_ms
        self._source_query = source_query or query_taskwarrior
        if pac_journal is None:
            from hyprial.pac.store import default_database_path
            pac_journal = FilePacJournal(default_database_path(state_dir))
        self._pac_journal = pac_journal
        self._event_sink = event_sink
        self._database = state_dir / "routines.sqlite3"
        self._projection = RoutineStore(self._database)
        self._condition = threading.Condition()
        self._results: dict[str, object] = {}
        self._waiters: set[str] = set()
        self._effects: Queue[RoutineEffect | object] = Queue(
            maxsize=max(1, mailbox_capacity * 2)
        )
        self._effect_ids: set[str] = set()
        self._effect_ids_lock = threading.Lock()
        self._generation = 0
        self._timer_sequence = 0
        self._closing = False
        self._effect_stop = threading.Event()
        self._writer_store: RoutineStore | None = None
        self._close_lock = threading.Lock()
        self._projection_closed = False
        self._writer_closed = False
        self._runtime = ActorRuntime()

        def handler_factory() -> RoutineRegistry:
            self._generation += 1
            if self._writer_store is not None:
                self._writer_store.close()
            store = RoutineStore(self._database)
            self._writer_store = store
            return RoutineRegistry(
                store=store,
                generation=self._generation,
                publish=self._publish,
                clock_ms=self._clock_ms,
                migrate_address=self._migrate_address,
                logger=self._logger,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name="routine-registry",
                handler_factory=handler_factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="state_authority",
            )
        )
        self._effect_thread = threading.Thread(
            target=self._effect_loop,
            name="hyprial-routine-effects",
            daemon=True,
        )
        self._effect_thread.start()

    def submit(self, command: object) -> PortAdmission:
        if self._closing:
            return PortAdmission.CLOSING
        admission = self._runtime.tell(self._handle, command)
        return {
            AdmissionResult.ACCEPTED: PortAdmission.ACCEPTED,
            AdmissionResult.OVERLOADED: PortAdmission.OVERLOADED,
            AdmissionResult.CLOSED: PortAdmission.CLOSING,
        }[admission]

    def add(self, *, yaml_text: str, owner: str) -> dict[str, object]:
        correlation = self._correlation()
        event = self._submit_wait(
            AddRoutineCommand(correlation, yaml_text, owner),
            correlation,
            RoutineMutationCompleted,
        )
        return event.result.to_payload()

    def list(self) -> dict[str, object]:
        return {"routines": [item.to_payload() for item in self.read_routines()]}

    def address_migrations(self) -> list[dict[str, object]]:
        """Ledger of stored-spec address rewrites (doctor/audit visibility)."""

        return [
            AddressMigrationProjection(
                routine=row.routine,
                field=row.field,
                before=row.before,
                after=row.after,
                migrated_at_ms=row.migrated_at_ms,
            ).to_payload()
            for row in self._projection.address_migrations()
        ]

    def status(self, *, name: str) -> dict[str, object]:
        projection = self.read_routine(name)
        if projection is None:
            raise RoutineServiceError("ROUTINE_NOT_FOUND", f"no such routine: {name}")
        return projection.to_payload()

    def remove(self, *, name: str) -> dict[str, object]:
        correlation = self._correlation()
        event = self._submit_wait(
            RemoveRoutineCommand(correlation, name),
            correlation,
            RoutineMutationCompleted,
        )
        return event.result.to_payload()

    def pause(self, *, name: str) -> dict[str, object]:
        correlation = self._correlation()
        event = self._submit_wait(
            PauseRoutineCommand(correlation, name),
            correlation,
            RoutineMutationCompleted,
        )
        return event.result.to_payload()

    def resume(self, *, name: str) -> dict[str, object]:
        correlation = self._correlation()
        event = self._submit_wait(
            ResumeRoutineCommand(correlation, name),
            correlation,
            RoutineMutationCompleted,
        )
        return event.result.to_payload()

    @property
    def migrated_u3(self) -> dict[str, tuple[str, ...]]:
        """What opening the store deleted for U3, so startup can report it.

        Allen, 2026-09-17, on the rows the retired dispatcher left behind:
        「迁移时直接删除」 -- a deletion, and NOT a silent one.  The store
        does the deleting and returns the ids; nothing else reads them, so
        without this the only trace of a dropped task would be its absence.
        """

        return self._projection.migrated_u3

    def recover(self) -> int:
        correlation = self._correlation()
        event = self._submit_wait(
            RecoverRoutinesCommand(correlation),
            correlation,
            RoutinesRecovered,
        )
        return event.adopted

    def submit_timer(self, observed_at_ms: int | None = None) -> PortAdmission:
        """Submit one generation-fenced cadence command to the Routine actor."""

        if self._closing:
            return PortAdmission.CLOSING
        self._timer_sequence += 1
        try:
            generation = self._runtime.snapshot(self._handle).generation
        except Exception:
            return PortAdmission.CLOSING
        return self.submit(
            RoutineTimerElapsedCommand(
                correlation_id=f"routine-timer-{uuid4().hex}",
                generation=generation,
                version=self._timer_sequence,
                observed_at_ms=(
                    self._clock_ms()
                    if observed_at_ms is None
                    else observed_at_ms
                ),
            )
        )

    def read_routine(self, name: str) -> RoutineProjection | None:
        snapshot = self._projection.snapshot(name)
        return None if snapshot is None else self._projection_of(snapshot)

    def read_routines(self) -> tuple[RoutineProjection, ...]:
        return tuple(
            self._projection_of(snapshot) for snapshot in self._projection.snapshots()
        )

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        report = self._runtime.drain(timeout=2.0)
        if not report.complete:
            self._runtime.stop(self._handle, timeout=0.0)
        self._effect_stop.set()
        try:
            self._effects.put_nowait(_STOP)
        except Full:
            pass
        self._effect_thread.join(timeout=2.0)
        if not self._effect_thread.is_alive():
            self._close_projection()
        if report.complete and self._writer_store is not None:
            self._close_writer()

    def _projection_of(self, snapshot: RoutineSnapshot) -> RoutineProjection:
        row = snapshot.routine
        schema_error: str | None = None
        try:
            produces = load_routine_text(
                row.yaml_text, label=f"stored routine {row.name}"
            ).produces
        except RoutineSchemaError as error:
            # Persisted schema faults are data, not a reason to make every
            # routine unreadable. Preserve the raw ownership marker when it
            # can be decoded, but keep the validation failure explicit.
            schema_error = str(error)
            produces = None
            try:
                raw = yaml.safe_load(row.yaml_text)
            except yaml.YAMLError:
                raw = None
            if isinstance(raw, dict) and isinstance(raw.get("produces"), str):
                produces = raw["produces"]
        return RoutineProjection(
            version=row.version,
            name=row.name,
            owner=row.owner,
            enabled=row.enabled,
            next_due_ms=row.next_due_ms,
            source_error_streak=row.source_error_streak,
            outcomes=tuple(str(item) for item in __import__("json").loads(row.outcomes)),
            in_flight=tuple(
                RoutineInFlightProjection(item.task_uuid, item.run_id, item.target)
                for item in snapshot.in_flight
            ),
            produces=produces,
            schema_error=schema_error,
            quarantine_reason=row.quarantine_reason,
        )

    def _submit_wait(
        self,
        command: object,
        correlation_id: str,
        expected: type[_ResultT],
    ) -> _ResultT:
        with self._condition:
            self._waiters.add(correlation_id)
        admission = self.submit(command)
        if admission is not PortAdmission.ACCEPTED:
            with self._condition:
                self._waiters.discard(correlation_id)
            raise RoutineServiceError(
                "ROUTINE_OVERLOADED"
                if admission is PortAdmission.OVERLOADED
                else "ROUTINE_CLOSING",
                f"routine registry admission: {admission}",
            )
        with self._condition:
            ready = self._condition.wait_for(
                lambda: correlation_id in self._results, timeout=30.0
            )
            if not ready:
                self._waiters.discard(correlation_id)
                self._results.pop(correlation_id, None)
                raise RoutineServiceError(
                    "ROUTINE_COMMAND_TIMEOUT",
                    f"routine command {correlation_id} did not settle",
                )
            result = self._results.pop(correlation_id)
            self._waiters.discard(correlation_id)
        if isinstance(result, PortCommandRejected):
            raise RoutineServiceError(result.code, result.detail)
        if not isinstance(result, expected):
            raise RoutineServiceError(
                "ROUTINE_PROTOCOL_ERROR",
                f"expected {expected.__name__}, got {type(result).__name__}",
            )
        return result

    def _publish(self, output: RegistryOutput) -> None:
        if isinstance(
            output,
            (RoutineSourceQueryEffect, RoutinePacEffect, RoutineAlarmEffect),
        ):
            self._enqueue_effect(output)
            return
        if self._event_sink is not None:
            self._event_sink(output)
        with self._condition:
            if output.correlation_id not in self._waiters:
                return
            self._results[output.correlation_id] = output
            self._condition.notify_all()

    def _enqueue_effect(self, effect: RoutineEffect) -> None:
        with self._effect_ids_lock:
            if effect.effect_id in self._effect_ids:
                return
            self._effect_ids.add(effect.effect_id)
        try:
            self._effects.put_nowait(effect)
        except Full:
            with self._effect_ids_lock:
                self._effect_ids.discard(effect.effect_id)

    def _effect_loop(self) -> None:
        try:
            while not self._effect_stop.is_set():
                try:
                    effect = self._effects.get(timeout=0.05)
                except Empty:
                    self._refill_effects()
                    continue
                if effect is _STOP:
                    return
                assert isinstance(
                    effect,
                    (RoutineSourceQueryEffect, RoutinePacEffect, RoutineAlarmEffect),
                )
                completion = self._execute_effect(effect)
                stale_generation = self._submit_completion(effect, completion)
                if stale_generation:
                    with self._effect_ids_lock:
                        self._effect_ids.discard(effect.effect_id)
                self._refill_effects()
        finally:
            self._close_projection()

    def _refill_effects(self) -> None:
        if self._effect_stop.is_set():
            return
        rows = self._projection.pending_effects(limit=10_000)
        pending_ids = {row.effect_id for row in rows}
        with self._effect_ids_lock:
            self._effect_ids.intersection_update(pending_ids)
        for row in rows:
            self._enqueue_effect(RoutineRegistry.decode_effect(row.payload))

    def _submit_completion(self, effect: RoutineEffect, completion: object) -> bool:
        while not self._effect_stop.is_set():
            admission = self._runtime.tell(self._handle, completion)
            if admission is AdmissionResult.ACCEPTED:
                return False
            try:
                generation = self._runtime.snapshot(self._handle).generation
            except Exception:
                generation = effect.generation
            if generation != effect.generation:
                return True
            time.sleep(0.01)
        return False

    def _execute_effect(self, effect: RoutineEffect) -> object:
        if isinstance(effect, RoutineSourceQueryEffect):
            return self._execute_source(effect)
        if isinstance(effect, RoutineAlarmEffect):
            code: str | None = None
            detail: str | None = None
            try:
                result = self._alarm.escalate(
                    to=effect.to,
                    text=effect.text,
                    reason="ROUTINE_ALARM",
                    conversation_id=f"routine:{effect.routine_name}",
                )
                if getattr(result, "status", "delivered") != "delivered":
                    code = f"ALARM_{str(getattr(result, 'status', 'failed')).upper()}"
                    detail = f"escalation {getattr(result, 'status', 'failed')}"
            except Exception as error:
                code, detail = type(error).__name__, str(error)
            return RoutinePacIoCompleted(
                effect.effect_id,
                effect.generation,
                effect.version,
                effect.routine_name,
                "",
                effect.operation,
                code=code,
                detail=detail,
            )
        return self._execute_pac(effect)

    def _execute_source(
        self, effect: RoutineSourceQueryEffect
    ) -> RoutineSourceQueryCompleted:
        tasks: tuple[RoutineSourceTaskProjection, ...] = ()
        code: str | None = None
        detail: str | None = None
        permanent = False
        try:
            source_tasks = (
                query_pac_journal(
                    self._pac_journal,
                    coordinator=effect.coordinator,
                    now_ms=self._clock_ms(),
                    idle_threshold_seconds=effect.source_idle_threshold_seconds,
                )
                if effect.source_kind == "pac-journal"
                else self._source_query(effect.source_filter)
            )
            tasks = tuple(
                RoutineSourceTaskProjection(item.uuid, item.description, item.tags)
                for item in source_tasks
            )
        except SourcePermanentError as error:
            code, detail, permanent = "ROUTINE_SOURCE_PERMANENT", str(error), True
        except SourceTransientError as error:
            code, detail = "ROUTINE_SOURCE_TRANSIENT", str(error)
        except Exception as error:
            code, detail = type(error).__name__, str(error)
        return RoutineSourceQueryCompleted(
            effect.effect_id,
            effect.generation,
            effect.version,
            effect.routine_name,
            tasks,
            code,
            detail,
            permanent,
        )

    def _execute_pac(self, effect: RoutinePacEffect) -> RoutinePacIoCompleted:
        """Run one PAC call for one task and report its settled state.

        A failure here is reported as ``code``/``detail`` and the registry
        counts it as an escalation: "could not read or build the graph" is a
        routine-level failure, exactly as the old "status read failed" was
        (hq-adjutant, 2026-09-14).
        """

        graph_id: str | None = None
        state: str | None = None
        code: str | None = None
        detail: str | None = None
        try:
            if effect.operation == "pac.status":
                assert effect.graph_id is not None
                result = self._pac.status(graph_id=effect.graph_id)
                graph_id = effect.graph_id
                state = str(result.get("state"))
                if state in {"done", "escalated"} and effect.sender is not None:
                    # Settled: retire the graph as its creator.  Closing never
                    # touches a flag, so the completion fact survives.
                    self._pac.close(graph_id=effect.graph_id, actor=effect.sender)
            else:
                assert effect.task_text is not None
                assert effect.target is not None
                assert effect.escalate_to is not None
                assert effect.sender is not None
                assert effect.timeout_seconds is not None
                result = self._pac.start_idempotent(
                    routine_name=effect.routine_name,
                    task_uuid=effect.task_uuid,
                    task_text=effect.task_text,
                    target=effect.target,
                    escalate_to=effect.escalate_to,
                    timeout_seconds=float(effect.timeout_seconds),
                    sender=effect.sender,
                )
                graph_id = str(result["graphId"])
                state = str(result.get("state", "running"))
        except Exception as error:
            code, detail = type(error).__name__, str(error)
        return RoutinePacIoCompleted(
            effect.effect_id,
            effect.generation,
            effect.version,
            effect.routine_name,
            effect.task_uuid,
            effect.operation,
            graph_id,
            state,
            code,
            detail,
        )

    @staticmethod
    def _correlation() -> str:
        return f"routine-corr-{uuid4().hex}"

    def _close_projection(self) -> None:
        with self._close_lock:
            if self._projection_closed:
                return
            self._projection_closed = True
            self._projection.close()

    def _close_writer(self) -> None:
        with self._close_lock:
            if self._writer_closed or self._writer_store is None:
                return
            self._writer_closed = True
            self._writer_store.close()


RoutineService = RoutineFacade

__all__ = [
    "AlarmSink",
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "RoutineFacade",
    "RoutineService",
    "RoutineServiceError",
    "SourceQuery",
    "PacPort",
]
